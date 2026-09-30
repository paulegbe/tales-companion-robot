"""
vision_node.py

Subscribes to /image_raw (published by usb_cam), runs YOLO detection
on each frame, and routes results:

    - person detections  -> face_recognition identity matching ->
                             entity created/updated in memory, logged
    - everything else    -> spoken/logged using YOLO's class label directly,
                             no identity tracking, no memory write

Publishes human-readable detection results to /vision_detections for
any downstream consumer (terminal logging now, screen overlay later).

Also publishes an annotated version of each processed frame (with YOLO's
bounding boxes and labels drawn on) to /vision_annotated, viewable with
rqt_image_view for visual debugging.

This node depends on MemoryManager for entity storage -- same instance
pattern as claude_bridge_node, but currently runs as a SEPARATE node
with its OWN MemoryManager instance. Since MemoryManager just opens
files on disk (SQLite + ChromaDB), both nodes reading/writing the same
data_dir is safe, but be aware they don't share Python state -- e.g.
"last unnamed entity" isn't currently passed between vision and
claude_bridge_node yet. That wiring (letting you SAY "that's Paul" and
having it apply to the most recently seen unnamed face) is a next step,
not built in this file.

CHANGES IN THIS VERSION:
    - Identity-merge fix. Unidentified faces now get their placeholder
      name from MemoryManager.create_unnamed_person(), which builds it
      from the database row id. The old in-memory counter reset on every
      launch, so the first new face after a restart was named
      unnamed_person_1, and get_or_create_entity() returned the EXISTING
      unnamed_person_1. A stranger's face was then attached to someone
      already stored.
    - Observations are throttled to one per person per minute. Logging
      every processed frame (~3 per second) grew the observations table by
      ~10,000 rows an hour with no new information.
    - The terminal only logs when the detection summary changes, instead
      of repeating the same line several times a second.
    - Clean Ctrl+C shutdown, and the memory connection is closed on exit.
    - Publishes /vision_people: a JSON snapshot of who is in view, with
      entity ids, on every processed frame (even when empty, so listeners
      can tell the data is fresh). claude_bridge_node uses it for its
      who_is_here and name_person tools.

/vision_people message format (std_msgs/String containing JSON):
    {
      "people": [
        {"entity_id": 7, "name": "Paul", "known": true},
        {"entity_id": 9, "name": "unnamed_person_9", "known": false}
      ],
      "faces_not_visible": 1    # people detected with no readable face
    }
    JSON in a String avoids a custom message package for now. A proper
    .msg interface package is the cleaner long-term version.
"""

import json

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import String
from cv_bridge import CvBridge
from ultralytics import YOLO
import face_recognition
import time

import cv2
import numpy as np

from companion_brain.memory_manager import MemoryManager


class VisionNode(Node):
    """
    Runs YOLO object detection + face recognition on each incoming
    camera frame, logs results to persistent memory, and publishes
    a readable summary plus an annotated debug image.
    """

    def __init__(self):
        super().__init__('vision_node')

        # Converts ROS 2 Image messages <-> OpenCV frames (numpy arrays)
        self.bridge = CvBridge()

        # Pretrained general object detector -- includes 'person' as
        # one of its 80 default COCO classes, along with cat, dog, etc.
        self.get_logger().info('Loading YOLO model...')
        self.yolo = YOLO('yolov8n.pt')

        # Same persistent memory store used by claude_bridge_node --
        # both nodes read/write the same SQLite + ChromaDB files on disk.
        self.memory = MemoryManager()

        # Throttle: only run detection every N frames, since usb_cam
        # publishes much faster than we need to process for this use
        # case, and YOLO + face matching isn't free on every single frame.
        self.frame_count = 0
        self.process_every_n_frames = 10

        # Observation throttle: entity_id -> last time a sighting was
        # written to memory. One observation per person per interval is
        # enough to answer "when did I last see them".
        self.observation_interval_s = 60.0
        self.last_observation = {}

        # Last summary printed to the terminal, so identical lines
        # aren't repeated every processed frame.
        self.last_summary = None

        self.subscription = self.create_subscription(
            Image,
            '/image_raw',
            self.handle_frame,
            10
        )

        # Human-readable detection summary (e.g. "Paul, cat")
        self.publisher_ = self.create_publisher(String, '/vision_detections', 10)

        # Annotated frame with bounding boxes drawn on, for visual debugging
        # via rqt_image_view -- separate from the detection summary above.
        self.annotated_publisher_ = self.create_publisher(Image, '/vision_annotated', 10)

        # Structured "who is in view" snapshot for claude_bridge_node.
        self.people_publisher_ = self.create_publisher(String, '/vision_people', 10)

        self.get_logger().info('Vision node ready.')

    def handle_frame(self, msg):
        """
        Callback fired on every incoming camera frame. Throttled to
        every Nth frame to keep CPU/GPU load reasonable -- face
        recognition especially is expensive to run at full frame rate.
        """
        self.frame_count += 1
        if self.frame_count % self.process_every_n_frames != 0:
            return

        # Convert the ROS 2 Image message into a plain OpenCV BGR frame
        frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')

        # Run YOLO detection -- returns bounding boxes + class labels
        # for everything it recognizes in the frame.
        results = self.yolo(frame, verbose=False)

        detections_summary = []
        people = {}             # entity_id -> {entity_id, name, known}
        faces_not_visible = 0

        for result in results:
            for box in result.boxes:
                class_id = int(box.cls[0])
                class_name = self.yolo.names[class_id]
                confidence = float(box.conf[0])

                # Skip low-confidence detections -- reduces false positives
                if confidence < 0.5:
                    continue

                x1, y1, x2, y2 = map(int, box.xyxy[0])

                if class_name == 'person':
                    label, person = self.identify_person(frame, x1, y1, x2, y2)
                    if person is None:
                        faces_not_visible += 1
                    else:
                        # Keyed by id, so one person matched twice in a
                        # frame is still listed once.
                        people[person['entity_id']] = person
                else:
                    # Non-person objects: just use YOLO's label directly,
                    # no identity tracking, no memory write.
                    label = class_name

                detections_summary.append(label)

        # Publish the annotated debug frame ONCE per processed frame,
        # not once per detected box -- .plot() already draws all boxes
        # from this frame's results in a single image.
        annotated_frame = results[0].plot()
        annotated_frame = np.ascontiguousarray(annotated_frame, dtype=np.uint8)
        annotated_msg = self.bridge.cv2_to_imgmsg(annotated_frame, encoding='bgr8')
        self.annotated_publisher_.publish(annotated_msg)

        # Publish the people snapshot every processed frame, even when
        # empty, so the bridge can tell fresh data from a stopped node.
        self.people_publisher_.publish(String(data=json.dumps({
            'people': list(people.values()),
            'faces_not_visible': faces_not_visible,
        })))

        if detections_summary:
            summary_text = ', '.join(detections_summary)

            # Only print when what's in view changes.
            if summary_text != self.last_summary:
                self.get_logger().info(f'Detected: {summary_text}')
                self.last_summary = summary_text

            out_msg = String()
            out_msg.data = summary_text
            self.publisher_.publish(out_msg)

    def identify_person(self, frame, x1, y1, x2, y2):
        """
        Given a bounding box known to contain a person (from YOLO),
        crops that region, generates a face encoding, and checks it
        against previously seen faces in memory.

        Returns (label, person):
            label  : human-readable text for logs, e.g. 'Paul' or
                     'someone new (unnamed_person_9)'
            person : {'entity_id', 'name', 'known'} for /vision_people,
                     or None when no face was readable
        On no match, a new entity is created and the encoding stored for
        future matching.
        """
        # Crop to the person's bounding box before running face detection --
        # narrows the search area and reduces false face detections
        # elsewhere in frame.
        person_crop = frame[y1:y2, x1:x2]
        if person_crop.size == 0:
            return 'person (face not visible)', None

        # face_recognition expects RGB, OpenCV gives BGR -- convert.
        rgb_crop = cv2.cvtColor(person_crop, cv2.COLOR_BGR2RGB)

        face_locations = face_recognition.face_locations(rgb_crop)
        if not face_locations:
            # YOLO found a person-shaped region, but no clear face in it
            # (turned away, too small, motion blur, etc.)
            return 'person (face not visible)', None

        face_encodings = face_recognition.face_encodings(rgb_crop, face_locations)
        if not face_encodings:
            return 'person (face not visible)', None

        # Only handling the first face found in this crop -- multiple
        # faces per person-box shouldn't normally happen since YOLO
        # already isolated one person region.
        encoding = face_encodings[0]

        matched_entity_id = self.memory.find_matching_face(encoding)

        if matched_entity_id is not None:
            # Known face -- look up their current name and record the
            # sighting (throttled, see log_sighting).
            name = self.memory.get_entity_name(matched_entity_id) or 'unknown'
            self.memory.touch_entity(matched_entity_id)
            self.log_sighting(matched_entity_id, 'Seen (face match)')
            return name, {
                'entity_id': matched_entity_id,
                'name': name,
                'known': not name.startswith('unnamed_person_'),
            }

        else:
            # New face -- create a placeholder entity and store its encoding
            # so it can be matched (and eventually named) going forward.
            # The name comes from the database row id, so it's unique
            # even across restarts.
            entity_id, placeholder_name = self.memory.create_unnamed_person()
            self.memory.store_face_embedding(entity_id, encoding)
            self.log_sighting(entity_id, 'First sighting, unidentified', force=True)

            return f'someone new ({placeholder_name})', {
                'entity_id': entity_id,
                'name': placeholder_name,
                'known': False,
            }

    def log_sighting(self, entity_id, description, force=False):
        """
        Writes an observation at most once per observation_interval_s per
        person. force=True always writes (used for first sightings).
        """
        now = time.monotonic()
        last = self.last_observation.get(entity_id)
        if force or last is None or now - last >= self.observation_interval_s:
            self.memory.log_observation(entity_id, description)
            self.last_observation[entity_id] = now

    def destroy_node(self):
        """Close the memory connection before shutting down."""
        self.memory.close()
        super().destroy_node()


def main(args=None):
    """Standard ROS 2 node entry point."""
    rclpy.init(args=args)
    node = VisionNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        # Ctrl+C mid-inference is the normal way to stop. Exit cleanly
        # instead of printing a traceback and failing.
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():  # Ctrl+C may have already shut rclpy down
            rclpy.shutdown()


if __name__ == '__main__':
    main()