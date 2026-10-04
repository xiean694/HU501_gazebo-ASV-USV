#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Point
import matplotlib.pyplot as plt
from threading import Lock

class TrajectoryPlotter(Node):
    def __init__(self):
        super().__init__('trajectory_plotter')

        # 缓存数据
        self.usv_x = []
        self.usv_y = []
        self.ref_x = []
        self.ref_y = []
        self.lock = Lock()

        # 订阅两个话题
        self.create_subscription(Point, '/usv/trajectory', self.usv_callback, 10)
        self.create_subscription(Point, '/usv/reference_trajectory', self.ref_callback, 10)

        # 定时刷新图像
        self.timer = self.create_timer(0.1, self.update_plot)

        # 设置交互式绘图
        plt.ion()
        self.fig, self.ax = plt.subplots()
        self.usv_line, = self.ax.plot([], [], 'b-', label='USV Trajectory')
        self.ref_line, = self.ax.plot([], [], 'r--', label='Reference Trajectory')
        self.ax.set_xlabel("X (m)")
        self.ax.set_ylabel("Y (m)")
        self.ax.legend()
        self.ax.grid(True)

    def usv_callback(self, msg: Point):
        with self.lock:
            self.usv_x.append(msg.x)
            self.usv_y.append(msg.y)

    def ref_callback(self, msg: Point):
        with self.lock:
            self.ref_x.append(msg.x)
            self.ref_y.append(msg.y)

    def update_plot(self):
        with self.lock:
            self.usv_line.set_data(self.usv_x, self.usv_y)
            self.ref_line.set_data(self.ref_x, self.ref_y)
        # 自动缩放
        self.ax.relim()
        self.ax.autoscale_view()
        plt.draw()
        plt.pause(0.001)


def main(args=None):
    rclpy.init(args=args)
    node = TrajectoryPlotter()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
        plt.ioff()
        plt.show()


if __name__ == '__main__':
    main()