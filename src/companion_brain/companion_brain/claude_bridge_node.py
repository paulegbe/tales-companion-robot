"""
claude_bridge_node.py

Bridges ROS 2 text input to the Claude API and back, with vision
capability exposed as a tool Claude can choose to use.

Flow:
    /user_input (String)  -->  ClaudeBridgeNode  -->  /companion_response (String)

The node:
    1. Loads a personality/system prompt from companion.md
    2. Pulls relevant memory context (facts + similar past turns)
    3. Sends the message + context + personality + a vision TOOL
       DEFINITION to Claude
    4. If Claude decides it needs to see, it requests the
       'capture_and_view_snapshot' tool -- the node captures the
       current camera frame, sends it back as a tool result, and
       Claude continues reasoning with the image in hand
    5. Parses [FACT: key=value] tags, stores them, strips them
    6. Publishes the final reply and stores the turn in memory

This mirrors how a person decides to look at something mid-conversation --
Claude chooses to look, rather than every message being checked against
a keyword list.
"""

import os
import re
import base64
import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
import cv2
import anthropic
from companion_brain.memory_manager import MemoryManager


# Tool definition Claude sees on every call. This describes what the
# tool does and when to use it -- Claude decides on its own, based on
# this description, whether a given user message warrants looking.
VISION_TOOL = {
    "name": "capture_and_view_snapshot",
    "description": (
        "Captures a single current frame from the robot's camera and "
        "returns it for viewing. Use this whenever answering the user's "
        "question requires knowing what is currently visible -- for "
        "example, questions about what they're wearing, holding, what "
        "object is in front of the camera, who is present, or the "
        "current state of the room. Do not use this for questions that "
        "don't require current visual information."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}


class ClaudeBridgeNode(Node):
    """
    ROS 2 node that connects user text input to the Claude API.
    Claude has access to a vision tool it can invoke mid-conversation
    when it determines a question requires seeing the current camera feed.
    """

    def __init__(self):
        super().__init__('claude_bridge_node')

        self.client = anthropic.Anthropic()
        self.model = 'claude-sonnet-4-5'
        self.conversation_history = []
        self.system_prompt = self.load_personality()
        self.memory = MemoryManager()

        # Camera frame buffer -- kept up to date continuously, so a
        # snapshot is available the instant Claude decides to look,
        # rather than waiting on a fresh frame at request time.
        self.bridge = CvBridge()
        self.latest_frame = None
        self.image_subscription = self.create_subscription(
            Image,
            '/image_raw',
            self.handle_image,
            10
        )

        self.subscription = self.create_subscription(
            String,
            '/user_input',
            self.handle_input,
            10
        )
        self.publisher_ = self.create_publisher(String, '/companion_response', 10)

        self.get_logger().info('Claude bridge node ready (vision tool enabled).')

    def handle_image(self, msg):
        """Continuously updates the latest camera frame in memory."""
        self.latest_frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')

    def load_personality(self):
        """Reads companion.md as plain text for use as the Claude system prompt."""
        personality_path = os.path.expanduser(
            '~/companion_ws/src/companion_brain/companion_brain/companion.md'
        )
        try:
            with open(personality_path, 'r') as f:
                return f.read()
        except FileNotFoundError:
            self.get_logger().warn('companion.md not found, using default prompt.')
            return 'You are a helpful robot companion.'

    def extract_and_store_facts(self, reply_text):
        """Parses [FACT: key=value] tags out of a reply, stores them, strips them."""
        fact_pattern = r'\[FACT:\s*([^=\]]+)=([^\]]+)\]'
        matches = re.findall(fact_pattern, reply_text)

        for key, value in matches:
            key = key.strip()
            value = value.strip()
            self.memory.store_fact(key, value)
            self.get_logger().info(f'Stored fact: {key} = {value}')

        return re.sub(fact_pattern, '', reply_text).strip()

    def capture_snapshot_base64(self):
        """
        Encodes the current camera frame as base64 JPEG, the format
        Claude's vision API expects. Returns None if no frame has
        arrived yet (e.g. camera node not running).
        """
        if self.latest_frame is None:
            return None
        success, buffer = cv2.imencode('.jpg', self.latest_frame)
        if not success:
            return None
        return base64.b64encode(buffer).decode('utf-8')

    def handle_input(self, msg):
        """
        Callback fired on every message published to /user_input.

        Sends the message to Claude along with the vision tool definition.
        If Claude's response includes a tool_use block requesting the
        snapshot, this method executes it locally, sends the image back
        as a tool_result, and calls Claude again to get the final answer
        that reasons over the image. Otherwise, the first response is
        used directly -- no unnecessary vision calls.
        """
        user_text = msg.data
        self.get_logger().info(f'Received: {user_text}')

        context_block = self.memory.build_context_block(user_text)
        full_system_prompt = self.system_prompt
        if context_block:
            full_system_prompt += '\n\n' + context_block

        self.conversation_history.append({'role': 'user', 'content': user_text})

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                system=full_system_prompt,
                tools=[VISION_TOOL],
                messages=self.conversation_history
            )

            # Check whether Claude decided it needs to use the vision tool.
            # stop_reason == 'tool_use' means the response contains a
            # request to call one of the tools we offered, rather than
            # a final text answer.
            if response.stop_reason == 'tool_use':
                self.get_logger().info('Claude requested a camera snapshot.')

                tool_use_block = next(
                    block for block in response.content if block.type == 'tool_use'
                )

                image_b64 = self.capture_snapshot_base64()

                if image_b64 is None:
                    tool_result_content = [{
                        "type": "text",
                        "text": "No camera frame is currently available."
                    }]
                else:
                    tool_result_content = [{
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64
                        }
                    }]

                # Append Claude's tool-use request, then our tool result,
                # to the conversation -- this is the required message
                # shape for the follow-up call to make sense of what
                # happened.
                self.conversation_history.append({
                    'role': 'assistant',
                    'content': response.content
                })
                self.conversation_history.append({
                    'role': 'user',
                    'content': [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content": tool_result_content
                    }]
                })

                # Second call -- Claude now has the image and produces
                # its actual answer, reasoning over what it sees.
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=500,
                    system=full_system_prompt,
                    tools=[VISION_TOOL],
                    messages=self.conversation_history
                )

            # Extract the final text reply, whether this was a direct
            # answer or the follow-up after using the vision tool.
            reply_text = ''
            for block in response.content:
                if block.type == 'text':
                    reply_text += block.text

            reply_text = self.extract_and_store_facts(reply_text)

            self.conversation_history.append({'role': 'assistant', 'content': reply_text})

            if len(self.conversation_history) > 20:
                self.conversation_history = self.conversation_history[-20:]

            self.memory.store_turn(user_text, reply_text)

            out_msg = String()
            out_msg.data = reply_text
            self.publisher_.publish(out_msg)

            self.get_logger().info(f'Claude: {reply_text}')

        except Exception as e:
            self.get_logger().error(f'Claude API error: {e}')


def main(args=None):
    """Standard ROS 2 node entry point."""
    rclpy.init(args=args)
    node = ClaudeBridgeNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()