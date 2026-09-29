#!/usr/bin/env python3
# hu501 全驱动 MPC 轨迹跟踪节点
# 用法:
#   ros2 run my_control mpc_node                                  # 默认正弦
#   ros2 run my_control mpc_node --ros-args -p trajectory:=circle # 圆形
# 可选参数: sine.amplitude / sine.period / sine.speed
#           circle.radius / circle.speed / duration
# 每次运行结束(跑完时长或 Ctrl+C)自动保存轨迹跟踪图 + CSV 到 mpc_results/
import rclpy
from rclpy.node import Node
from std_msgs.msg import Float64
from sensor_msgs.msg import NavSatFix
from sensor_msgs.msg import Imu
import casadi as ca
import numpy as np
import math
import os
import csv
from datetime import datetime

import matplotlib
matplotlib.use('Agg')  # 无显示环境也能保存图片
import matplotlib.pyplot as plt


def wrap_angle(a):
    """任意角wrap到 [-pi, pi]"""
    return math.atan2(math.sin(a), math.cos(a))


class MPCNode(Node):
    def __init__(self):
        super().__init__('mpc_node')
        # 使用仿真时钟(/clock),轨迹时间与船的运动保持一致(须在建 timer 前设置)
        self.set_parameters([
            rclpy.parameter.Parameter('use_sim_time',
                                      rclpy.Parameter.Type.BOOL, True)])
        # ---------- 参数 ----------
        self.declare_parameter('trajectory', 'sine')      # 'sine' | 'circle'
        self.declare_parameter('duration', 0.0)           # <=0: 自动(正弦2周期/圆1圈)
        self.declare_parameter('sine.amplitude', 5.0)     # 正弦幅值 [m]
        self.declare_parameter('sine.period', 40.0)       # 正弦周期 [s]
        self.declare_parameter('sine.speed', 0.4)         # 前进速度 [m/s]
        self.declare_parameter('circle.radius', 8.0)      # 圆半径 [m]
        self.declare_parameter('circle.speed', 0.5)       # 线速度 [m/s]
        self.declare_parameter('heading0', float('nan'))  # 起始航向[deg],NaN=用当前船头
        self.declare_parameter('settle_speed', 0.3)       # 起步前等船速低于此值 [m/s]

        self.traj_type = self.get_parameter('trajectory').value
        self.A = float(self.get_parameter('sine.amplitude').value)
        self.T_s = float(self.get_parameter('sine.period').value)
        self.vx = float(self.get_parameter('sine.speed').value)
        self.R = float(self.get_parameter('circle.radius').value)
        self.vc = float(self.get_parameter('circle.speed').value)
        self.duration = float(self.get_parameter('duration').value)
        self.heading0_cmd = float(self.get_parameter('heading0').value)
        self.settle_speed = float(self.get_parameter('settle_speed').value)

        # 自动时长:正弦 2 个周期,圆整整 1 圈
        if self.duration <= 0.0:
            if self.traj_type == 'circle':
                self.duration = 2.0 * math.pi * self.R / self.vc
            else:
                self.duration = 2.0 * self.T_s

        # ---------- hu501 全驱动推进器 ----------
        # t1/t2 主推(前后, y=±0.23), t3/t4 侧推(左右, x=±0.44)
        self.thrust_pubs = [
            self.create_publisher(Float64, f'/hu501/thrusters/t{i}/thrust', 10)
            for i in range(1, 5)
        ]
        self.create_subscription(NavSatFix, '/hu501/sensors/gps/gps/fix',
                                 self.gps_callback, 10)
        self.create_subscription(Imu, '/hu501/sensors/imu/imu/data',
                                 self.imu_callback, 10)
        self.timer = self.create_timer(0.1, self.timer_callback)  # 10 Hz

        self.get_logger().info(
            f"MPC Node: trajectory={self.traj_type}, duration={self.duration:.1f}s")

        # ---------- 动力学模型参数 ----------
        # 质量/惯量来自 URDF: m=22.584kg, Izz=2.07173
        self.m11 = 22.584
        self.m22 = 22.584
        self.m33 = 2.07173
        # 推力增益与水动力阻力: 2026-09-25 静推力标定(cmd=10/20/40 阶跃,
        # 稳态 u = 0.87/1.33/2.0 m/s)拟合得 F≈kt*cmd, D(u)≈Xu1*u+Xu2*u|u|
        self.declare_parameter('kt', 0.35)          # 推力增益 [N/cmd]
        self.declare_parameter('xu1', 3.5)          # 纵向线性阻尼 [N*s/m]
        self.declare_parameter('xu2', 5.3)          # 纵向二次阻尼 [N*s^2/m^2]
        self.declare_parameter('yv1', 6.0)          # 横向线性阻尼
        self.declare_parameter('yv2', 3.0)          # 横向二次阻尼
        self.declare_parameter('nr', 17.0)          # 艏摇线性阻尼 [N*m*s]
        self.kt = float(self.get_parameter('kt').value)
        self.Xu1 = float(self.get_parameter('xu1').value)
        self.Xu2 = float(self.get_parameter('xu2').value)
        self.Yv1 = float(self.get_parameter('yv1').value)
        self.Yv2 = float(self.get_parameter('yv2').value)
        self.Nr = float(self.get_parameter('nr').value)
        self.thruster_max = 40.0

        # ---------- 状态 ----------
        self.origin_set = False
        self.psi_valid = False
        self.latitude = None
        self.longitude = None
        self.origin_lat = None
        self.origin_lon = None
        self.last_gps_t = None      # 上次 GPS 更新时刻(节点时钟)
        self.x = 0.0
        self.y = 0.0
        self.last_gx = None         # 上一次 GPS 平面坐标(仅在 GPS 更新时差分)
        self.last_gy = None
        self.last_gx_t = None       # 上一次 GPS 平面坐标对应时刻
        self.u = 0.0
        self.v = 0.0
        self.r = 0.0
        self.psi = 0.0

        # ---------- MPC 参数 ----------
        self.dt = 0.1               # 控制周期
        self.mpc_dt = 0.2           # 预测模型积分步长(与参考轨迹保持一致)
        self.N = 10                 # 预测步数
        # 状态权重(对应 [x, y, psi, u, v, r] 六维):
        #   x/y 位置跟踪;psi 航向跟上路径切向;u 保持正前速(防倒车/横滑)
        self.Q = np.array([600.0, 600.0, 300.0, 40.0, 0.0, 0.0])
        self.Qf = np.array([1200.0, 1200.0, 600.0, 80.0, 0.0, 0.0])   # 末端权重
        self.Rw = 0.1                          # 推力权重

        model = self.__build_model_()
        self.solver, self.x_dim, self.u_dim = self.__build_mpc_controller__(model)
        self.u_warm = np.zeros(self.N * 4)      # 热启动初值

        # ---------- 运行记录 ----------
        self.t0 = None             # 轨迹起始时刻(节点时钟秒)
        self.psi0 = None           # 初始航向,参考轨迹按它旋转对准
        self.finished = False
        self.log_t = []
        self.log_ref = []          # (x, y, psi)
        self.log_act = []          # (x, y, psi)
        self.log_u = []            # (t1, t2, t3, t4)
        self.result_dir = './vrx_ws/mpc_results'
        os.makedirs(self.result_dir, exist_ok=True)

    # ================= 动力学模型 =================
    def __build_model_(self):
        x = ca.SX.sym('x')
        y = ca.SX.sym('y')
        psi = ca.SX.sym('psi')
        u = ca.SX.sym('u')
        v = ca.SX.sym('v')
        r = ca.SX.sym('r')
        T1 = ca.SX.sym('T1')
        T2 = ca.SX.sym('T2')
        T3 = ca.SX.sym('T3')
        T4 = ca.SX.sym('T4')

        # 推进器 -> 广义力(cmd 单位,乘 kt 转成牛顿;t3/t4 正推力 = -y 向右舷)
        kt = self.kt
        fu = kt * (T1 + T2)
        fv = -kt * (T3 + T4)
        tr = kt * (-0.23 * T1 + 0.23 * T2 - 0.44 * T3 + 0.44 * T4)

        # 阻力 = 线性 + 二次(标定值)
        du = self.Xu1 * u + self.Xu2 * u * ca.fabs(u)
        dv = self.Yv1 * v + self.Yv2 * v * ca.fabs(v)

        x_dot = u * ca.cos(psi) - v * ca.sin(psi)
        y_dot = u * ca.sin(psi) + v * ca.cos(psi)
        psi_dot = r
        u_dot = (fu - du + self.m22 * v * r) / self.m11
        v_dot = (fv - dv - self.m11 * u * r) / self.m22
        r_dot = (tr - self.Nr * r + (self.m11 - self.m22) * u * v) / self.m33

        state = ca.vertcat(x, y, psi, u, v, r)
        state_dot = ca.vertcat(x_dot, y_dot, psi_dot, u_dot, v_dot, r_dot)
        control = ca.vertcat(T1, T2, T3, T4)
        return ca.Function('f', [state, control], [state_dot])

    # ================= MPC 优化问题 =================
    def __build_mpc_controller__(self, model):
        N = self.N
        dt = self.mpc_dt
        X = ca.SX.sym('X', 6, N + 1)
        U = ca.SX.sym('U', 4, N)
        P = ca.SX.sym('P', 6 * (N + 1), 1)
        x_dim = X.shape[0]
        u_dim = U.shape[0]

        X[:, 0] = P[0:x_dim]
        cost = 0
        for k in range(N):
            e = X[:, k] - P[x_dim * (k + 1): x_dim * (k + 2)]
            # 航向差 wrap 到 [-pi, pi],避免整圈跟踪时代价函数跳变
            e_psi = ca.atan2(ca.sin(e[2]), ca.cos(e[2]))
            Qk = self.Q if k < N - 1 else self.Qf
            # 逐项加权(航向差已 wrap;v、r 权重目前为 0,需要时可启用)
            cost += Qk[0] * e[0] ** 2 + Qk[1] * e[1] ** 2 + \
                Qk[2] * e_psi ** 2 + Qk[3] * e[3] ** 2 + \
                Qk[4] * e[4] ** 2 + Qk[5] * e[5] ** 2
            cost += self.Rw * ca.sumsqr(U[:, k])
            X[:, k + 1] = X[:, k] + dt * model(X[:, k], U[:, k])

        OPT_variables = U.reshape((-1, 1))
        nlp_prob = {'f': cost, 'x': OPT_variables, 'p': P}
        opts = {'ipopt.print_level': 0, 'print_time': 0,
                'ipopt.max_iter': 100}
        solver = ca.nlpsol('solver', 'ipopt', nlp_prob, opts)
        return solver, x_dim, u_dim

    # ================= 参考轨迹 =================
    def __ref_raw(self, t):
        """未旋转的参考状态 [x, y, psi, u, v, r]"""
        if self.traj_type == 'circle':
            w = self.vc / self.R                     # 角速度
            th = w * t
            x = self.R * math.sin(th)
            y = self.R * (1.0 - math.cos(th))        # 起点(0,0), 初速沿 +x, 逆时针
            psi = th                                 # 切向航向
            return np.array([x, y, psi, self.vc, 0.0, w])
        else:  # sine: 沿 +x 前进, y 方向正弦
            w = 2.0 * math.pi / self.T_s
            x = self.vx * t
            y = self.A * math.sin(w * t)
            dx = self.vx
            dy = self.A * w * math.cos(w * t)
            psi = math.atan2(dy, dx)
            spd = math.hypot(dx, dy)
            # r_ref = dpsi/dt 数值差分
            eps = 1e-3
            psi_p = math.atan2(self.A * w * math.cos(w * (t + eps)), self.vx)
            r = wrap_angle(psi_p - psi) / eps
            return np.array([x, y, psi, spd, 0.0, r])

    def reference(self, t):
        """按初始航向 psi0 旋转后的参考状态"""
        ref = self.__ref_raw(t)
        c, s = math.cos(self.psi0), math.sin(self.psi0)
        x = c * ref[0] - s * ref[1]
        y = s * ref[0] + c * ref[1]
        psi = ref[2] + self.psi0
        return np.array([x, y, psi, ref[3], ref[4], ref[5]])

    # ================= 坐标变换 =================
    def deg2rad(self, deg):
        return deg * np.pi / 180.0

    def gps_to_xy(self, lat, lon):
        """经纬度 -> 当地 ENU 平面坐标(等距圆柱投影,避免墨卡托 y 尺度误差)"""
        R = 6378137.0
        x = R * math.cos(self.deg2rad(self.origin_lat)) * \
            self.deg2rad(lon - self.origin_lon)
        y = R * self.deg2rad(lat - self.origin_lat)
        return x, y

    # ================= 传感器回调 =================
    def gps_callback(self, msg):
        lat, lon = msg.latitude, msg.longitude
        if lat == 0.0 and lon == 0.0:
            return
        self.latitude = lat
        self.longitude = lon
        now = self.get_clock().now().nanoseconds / 1e9
        self.last_gps_t = now
        if not self.origin_set:
            self.origin_lat = lat
            self.origin_lon = lon
            self.origin_set = True
            self.x, self.y = 0.0, 0.0
            self.last_gx, self.last_gy, self.last_gx_t = 0.0, 0.0, now
            self.get_logger().info(
                f"GPS origin set: lat={lat:.6f}, lon={lon:.6f}")
            return
        # 平面坐标
        gx, gy = self.gps_to_xy(lat, lon)
        self.x, self.y = gx, gy
        # 速度:仅在 GPS 真正更新时差分(ENU -> 船体系),低通滤波
        if self.last_gx_t is not None:
            dt_gps = now - self.last_gx_t
            if dt_gps > 0.05 and (abs(gx - self.last_gx) > 1e-9 or
                                  abs(gy - self.last_gy) > 1e-9):
                vx = (gx - self.last_gx) / dt_gps
                vy = (gy - self.last_gy) / dt_gps
                u_new = vx * math.cos(self.psi) + vy * math.sin(self.psi)
                v_new = -vx * math.sin(self.psi) + vy * math.cos(self.psi)
                alpha = 0.5
                self.u = alpha * u_new + (1.0 - alpha) * self.u
                self.v = alpha * v_new + (1.0 - alpha) * self.v
        self.last_gx, self.last_gy, self.last_gx_t = gx, gy, now

    def imu_callback(self, msg):
        qx, qy, qz, qw = (msg.orientation.x, msg.orientation.y,
                          msg.orientation.z, msg.orientation.w)
        self.psi = math.atan2(2.0 * (qw * qz + qx * qy),
                              1.0 - 2.0 * (qy * qy + qz * qz))
        self.r = msg.angular_velocity.z
        if not self.psi_valid:
            self.psi_valid = True

    # ================= 主循环 =================
    def timer_callback(self):
        if self.finished:
            return
        now = self.get_clock().now().nanoseconds / 1e9

        if not self.origin_set or not self.psi_valid:
            return
        if self.t0 is None:
            # ---- 待机:预对准 + 等船静止 ----
            if self.psi0 is None:
                # 起始航向:参数指定,否则用当前船头
                if not math.isnan(self.heading0_cmd):
                    self.psi0 = math.radians(self.heading0_cmd)
                else:
                    self.psi0 = self.psi
                self.anchor = (self.x, self.y)   # 待机锚点(当前位置)
                self.get_logger().info(
                    f"待机对准: 目标航向 {math.degrees(self.reference(0)[2]):.1f} deg, "
                    f"当前 {math.degrees(self.psi):.1f} deg")
            psi_path0 = self.reference(0)[2]
            err_h = wrap_angle(psi_path0 - self.psi)
            spd = math.hypot(self.u, self.v)
            aligned = abs(err_h) < math.radians(10.0) and spd < self.settle_speed
            if not aligned:
                # 待机参考:锚点悬停 + 转向路径起始切向
                hold = np.array([self.anchor[0], self.anchor[1], psi_path0,
                                 0.0, 0.0, 0.0])
                ref_traj = np.concatenate([hold] * self.N)
                p = np.concatenate((np.array([self.x, self.y, self.psi,
                                              self.u, self.v, self.r]),
                                    ref_traj))
                lbx = -self.thruster_max * np.ones(self.N * 4)
                ubx = self.thruster_max * np.ones(self.N * 4)
                sol = self.solver(x0=self.u_warm, p=p, lbx=lbx, ubx=ubx)
                u_opt = sol['x'].full().flatten()
                self.u_warm = np.concatenate([u_opt[4:], u_opt[-4:]])
                for pub, val in zip(self.thrust_pubs, u_opt[:4]):
                    pub.publish(Float64(data=float(val)))
                self.get_logger().info(
                    f"待机对准中: 航向差 {math.degrees(err_h):+.1f} deg, "
                    f"速度 {spd:.2f} m/s", throttle_duration_sec=3.0)
                return
            self.t0 = now
            # 原点重设到当前位置:参考轨迹从船正下方开始
            self.origin_lat, self.origin_lon = self.latitude, self.longitude
            self.x = self.y = 0.0
            self.last_gx = self.last_gy = 0.0
            self.get_logger().info(
                f"Start tracking: {self.traj_type}, psi0={math.degrees(self.psi0):.1f} deg")

        t = now - self.t0
        if t >= self.duration:
            self.finish()
            return

        # --- 位置/速度已由 gps_callback / imu_callback 维护 ---
        # GPS 超过 1s 无更新则保持上一拍推力,跳过求解
        if self.last_gps_t is None or now - self.last_gps_t > 1.0:
            self.get_logger().warn("GPS 数据过期,保持推力", throttle_duration_sec=5.0)
            return

        # --- 参考轨迹(绝对时间 + mpc_dt 间隔,与模型积分一致) ---
        ref_traj = []
        for i in range(self.N):
            ref_traj.append(self.reference(t + i * self.mpc_dt))
        ref_traj = np.concatenate(ref_traj)
        p = np.concatenate((np.array([self.x, self.y, self.psi,
                                      self.u, self.v, self.r]),
                            ref_traj))

        lbx = -self.thruster_max * np.ones(self.N * 4)
        ubx = self.thruster_max * np.ones(self.N * 4)

        sol = self.solver(x0=self.u_warm, p=p,
                          lbx=lbx, ubx=ubx)
        u_opt = sol['x'].full().flatten()
        # 热启动: 后移一步,末尾重复
        self.u_warm = np.concatenate([u_opt[4:], u_opt[-4:]])

        t1, t2, t3, t4 = u_opt[0], u_opt[1], u_opt[2], u_opt[3]
        for pub, val in zip(self.thrust_pubs, (t1, t2, t3, t4)):
            pub.publish(Float64(data=float(val)))

        ref_now = self.reference(t)
        self.log_t.append(t)
        self.log_ref.append(ref_now[:3])
        self.log_act.append([self.x, self.y, self.psi, self.u, self.v, self.r])
        self.log_u.append([t1, t2, t3, t4])

        if len(self.log_t) % 50 == 0:
            err = math.hypot(self.x - ref_now[0], self.y - ref_now[1])
            self.get_logger().info(
                f"t={t:5.1f}s pos=({self.x:6.2f},{self.y:6.2f}) "
                f"ref=({ref_now[0]:6.2f},{ref_now[1]:6.2f}) err={err:5.2f}m")

    # ================= 结束:停车 + 画图 =================
    def finish(self, interrupted=False):
        if self.finished:
            return
        self.finished = True
        # 先保存数据(最重要),再尽力停车
        self.save_results(interrupted)
        try:
            import time as _time
            for _ in range(10):
                for pub in self.thrust_pubs:
                    pub.publish(Float64(data=0.0))
                _time.sleep(0.02)
            self.timer.cancel()
        except Exception as e:
            self.get_logger().warn(f"停车发布失败(可能已在关闭): {e}")
        try:
            rclpy.shutdown()
        except Exception:
            pass

    def save_results(self, interrupted=False):
        if len(self.log_t) < 10:
            self.get_logger().warn("数据太少,不画图")
            return
        t = np.array(self.log_t)
        ref = np.array(self.log_ref)
        act = np.array(self.log_act)
        uu = np.array(self.log_u)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        tag = f"mpc_{self.traj_type}_{stamp}"

        # ---- CSV ----
        csv_path = os.path.join(self.result_dir, tag + '.csv')
        with open(csv_path, 'w', newline='') as f:
            wr = csv.writer(f)
            wr.writerow(['t', 'ref_x', 'ref_y', 'ref_psi', 'act_x', 'act_y',
                         'act_psi', 'act_u', 'act_v', 'act_r',
                         't1', 't2', 't3', 't4'])
            for i in range(len(t)):
                wr.writerow([f"{t[i]:.3f}"] + [f"{q:.4f}" for q in
                             list(ref[i]) + list(act[i]) + list(uu[i])])

        # ---- RMS 误差(跳过前 10s 过渡过程) ----
        mask = t > 10.0
        if mask.sum() > 5:
            err = np.hypot(act[mask, 0] - ref[mask, 0],
                           act[mask, 1] - ref[mask, 1])
            rms = float(np.sqrt(np.mean(err ** 2)))
        else:
            rms = float('nan')

        # ---- 画图 ----
        fig, axs = plt.subplots(2, 2, figsize=(13, 9))
        fig.suptitle(f"hu501 MPC {self.traj_type} tracking"
                     f"{' (interrupted)' if interrupted else ''}"
                     f"   RMS err(t>10s) = {rms:.2f} m", fontsize=13)

        ax = axs[0, 0]
        ax.plot(ref[:, 0], ref[:, 1], 'r--', lw=1.5, label='reference')
        ax.plot(act[:, 0], act[:, 1], 'b-', lw=1.5, label='hu501 actual')
        ax.plot(act[0, 0], act[0, 1], 'g^', ms=10, label='start')
        ax.set_aspect('equal')
        ax.set_xlabel('x [m]')
        ax.set_ylabel('y [m]')
        ax.set_title('trajectory (local ENU)')
        ax.grid(True)
        ax.legend()

        ax = axs[0, 1]
        ax.plot(t, ref[:, 0], 'r--', label='x ref')
        ax.plot(t, act[:, 0], 'b-', label='x act')
        ax.plot(t, ref[:, 1], 'm--', label='y ref')
        ax.plot(t, act[:, 1], 'c-', label='y act')
        ax.set_xlabel('t [s]')
        ax.set_ylabel('position [m]')
        ax.set_title('position vs time')
        ax.grid(True)
        ax.legend(fontsize=8)

        ax = axs[1, 0]
        ax.plot(t, np.degrees(wrap_vec(ref[:, 2])), 'r--', label='psi ref')
        ax.plot(t, np.degrees(wrap_vec(act[:, 2])), 'b-', label='psi act')
        ax.set_xlabel('t [s]')
        ax.set_ylabel('heading [deg]')
        ax.set_title('heading vs time')
        ax.grid(True)
        ax.legend()

        ax = axs[1, 1]
        for i, nm in enumerate(('t1', 't2', 't3', 't4')):
            ax.plot(t, uu[:, i], label=nm)
        ax.set_xlabel('t [s]')
        ax.set_ylabel('thrust cmd')
        ax.set_title('thruster commands')
        ax.grid(True)
        ax.legend(fontsize=8)

        fig.tight_layout(rect=[0, 0, 1, 0.96])
        png_path = os.path.join(self.result_dir, tag + '.png')
        fig.savefig(png_path, dpi=120)
        plt.close(fig)
        self.get_logger().info(f"结果已保存: {png_path}")
        self.get_logger().info(f"CSV 已保存: {csv_path}")
        self.get_logger().info(f"RMS 跟踪误差(t>10s): {rms:.2f} m")
        print(f"\n[RESULT] {png_path}")


def wrap_vec(a):
    return np.arctan2(np.sin(a), np.cos(a))


def main(args=None):
    rclpy.init(args=args)
    node = MPCNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        node.finish(interrupted=True)
    except Exception as e:
        # 任何异常也要把已跑的数据画出来
        print(f"[ERROR] {e!r}")
        node.finish(interrupted=True)
    finally:
        if rclpy.ok():
            node.destroy_node()
            rclpy.shutdown()


if __name__ == '__main__':
    main()
