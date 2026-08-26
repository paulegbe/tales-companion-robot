"""
text_input_node.py

Temporary stand-in for voice input. Reads text typed in the terminal
and publishes it to /user_input, the same topic a future speech-to-text
node will publish to once the mic array is wired up.

This lets the rest of the pipeline (claude_bridge_node, memory, etc.)
be built and tested now, without waiting on audio hardware. When the
mic arrives, this node gets replaced -- nothing downstream needs to change,
since they only care about messages arriving on /user_input.
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class TextInputNode(Node):
    """Blocking terminal input loop that publishes each line to /user_input."""

    def __init__(self):
        super().__init__('text_input_node')
        self.publisher_ = self.create_publisher(String, '/user_input', 10)
        self.get_logger().info('Type a message and press Enter. Ctrl+C to quit.')
        self.run_loop()

    def run_loop(self):
        """
        Runs a blocking input() loop for as long as the node is alive.
        Note: this blocks rclpy's normal spin cycle -- fine for a
        single-purpose test node, but not a pattern to reuse for nodes
        that also need to process incoming messages.
        """
        try:
            while rclpy.ok():
                text = input('You: ')
                if not text.strip():
                    continue
                msg = String()
                msg.data = text
                self.publisher_.publish(msg)
        except KeyboardInterrupt:
            pass


def main(args=None):
    rclpy.init(args=args)
    node = TextInputNode()
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()