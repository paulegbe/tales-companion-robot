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
"""

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import String
from cv_bridge import CvBridge
from ultralytics import YOLO
import face_recognition
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

        # Counter for naming unidentified people until they're named
        # by the user -- e.g. 'unnamed_person_1', 'unnamed_person_2'.
        self.unnamed_person_counter = 0

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
                    label = self.identify_person(frame, x1, y1, x2, y2)
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

        if detections_summary:
            summary_text = ', '.join(detections_summary)
            self.get_logger().info(f'Detected: {summary_text}')

            out_msg = String()
            out_msg.data = summary_text
            self.publisher_.publish(out_msg)

    def identify_person(self, frame, x1, y1, x2, y2):
        """
        Given a bounding box known to contain a person (from YOLO),
        crops that region, generates a face encoding, and checks it
        against previously seen faces in memory.

        Returns a human-readable label: either a known name, or
        'someone new (unnamed_person_N)' if no match is found -- in
        which case a new entity is created and the encoding stored
        for future matching.
        """
        # Crop to the person's bounding box before running face detection --
        # narrows the search area and reduces false face detections
        # elsewhere in frame.
        person_crop = frame[y1:y2, x1:x2]

        # face_recognition expects RGB, OpenCV gives BGR -- convert.
        rgb_crop = cv2.cvtColor(person_crop, cv2.COLOR_BGR2RGB)

        face_locations = face_recognition.face_locations(rgb_crop)
        if not face_locations:
            # YOLO found a person-shaped region, but no clear face in it
            # (turned away, too small, motion blur, etc.)
            return 'person (face not visible)'

        face_encodings = face_recognition.face_encodings(rgb_crop, face_locations)
        if not face_encodings:
            return 'person (face not visible)'

        # Only handling the first face found in this crop -- multiple
        # faces per person-box shouldn't normally happen since YOLO
        # already isolated one person region.
        encoding = face_encodings[0]

        matched_entity_id = self.memory.find_matching_face(encoding)

        if matched_entity_id is not None:
            # Known face -- look up their current name and log this sighting
            cur = self.memory.conn.cursor()
            cur.execute('SELECT name FROM entities WHERE id=?', (matched_entity_id,))
            row = cur.fetchone()
            name = row[0] if row else 'unknown'

            self.memory.log_observation(matched_entity_id, 'Seen (face match)')
            return name

        else:
            # New face -- create a placeholder entity and store its encoding
            # so it can be matched (and eventually named) going forward.
            self.unnamed_person_counter += 1
            placeholder_name = f'unnamed_person_{self.unnamed_person_counter}'

            entity_id = self.memory.get_or_create_entity('person', placeholder_name)
            self.memory.store_face_embedding(entity_id, encoding)
            self.memory.log_observation(entity_id, 'First sighting, unidentified')

            return f'someone new ({placeholder_name})'


def main(args=None):
    """Standard ROS 2 node entry point."""
    rclpy.init(args=args)
    node = VisionNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()