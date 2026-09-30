from setuptools import find_packages, setup

package_name = 'companion_brain'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='villager',
    maintainer_email='villager@todo.todo',
    description='TODO: Package description',
    license='TODO: License declaration',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'claude_bridge_node = companion_brain.claude_bridge_node:main',
            'text_input_node = companion_brain.text_input_node:main',
             'vision_node = companion_brain.vision_node:main',
             'tts_node = companion_brain.tts_node:main',
             'stt_node = companion_brain.stt_node:main',
        ],
    },
)
