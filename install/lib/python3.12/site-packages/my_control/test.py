#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from std_msgs.msg import Float64
import sys, tty, termios, select

def get_key(timeout=0.1):

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)
        r,_,_ = select.select([fd],[],[],timeout)
        if r:
            return sys.stdin.read(1)
        else:
            return ''
    finally:
        termios.tcsetattr(fd,termios.TCSADRAIN,old)


class WAMVKeyboard(Node):

    def __init__(self):
        super().__init__('wamv_keyboard')

        # 注意话题名是 thrusters（复数），必须与 hu501 URDF 中定义一致
        self.left_pub = self.create_publisher(Float64,'/hu501/thrusters/t1/thrust', 10)
        self.right_pub = self.create_publisher(Float64,'/hu501/thrusters/t2/thrust', 10)
        self.front_pub = self.create_publisher(Float64,'/hu501/thrusters/t3/thrust', 10)
        self.back_pub = self.create_publisher(Float64,'/hu501/thrusters/t4/thrust', 10)

        self.get_logger().info("""
======= Keyboard hu501 =======
W forward      前进 (t1+t2)
S backward     后退 (t1+t2)
A left turn    左转 (主推差速+首尾侧推反向)
D right turn   右转 (主推差速+首尾侧推反向)
Z slide right  右移 (t3+t4)
X slide left   左移 (t3+t4)
Q quit         退出
==============================
""")

    def set_thrust(self,L,R,F,B):
        # Float64.data 必须是 float，传 int 会触发断言崩溃
        self.left_pub.publish(Float64(data=float(L)))
        self.right_pub.publish(Float64(data=float(R)))
        self.front_pub.publish(Float64(data=float(F)))
        self.back_pub.publish(Float64(data=float(B)))

    def run(self):

        try:
            while rclpy.ok():

                key = get_key()

                # ===============================
                # 有键盘输入
                # ===============================

                if key == 'w':
                    self.set_thrust(20.0, 20.0, 0.0, 0.0)

                elif key == 's':
                    self.set_thrust(-10.0, -10.0, 0.0, 0.0)

                elif key == 'a':
                    # 左转: τ = 0.23*(t2-t1) + 0.44*(t4-t3)
                    self.set_thrust(10.0, 30.0, -20.0, 20.0)

                elif key == 'd':
                    # 右转
                    self.set_thrust(30.0, 15.0, 20.0, -20.0)

                elif key == 'z':
                    self.set_thrust(0.0, 0.0, 20.0, 20.0)

                elif key == 'x':
                    self.set_thrust(0.0, 0.0, -10.0, -10.0)

                elif key == 'q':
                    break

                # ===============================
                # 没有按键 → 立刻停车
                # ===============================

                elif key == '':
                    self.set_thrust(0.0,0.0,0.0,0.0)

                rclpy.spin_once(self,timeout_sec=0)

        finally:
            self.set_thrust(0.0,0.0,0.0,0.0)


def main():

    rclpy.init()
    node=WAMVKeyboard()

    try:
        node.run()
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__=='__main__':
    main()
