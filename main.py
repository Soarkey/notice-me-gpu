# -*- encoding: utf-8 -*-

import datetime as dt
import json
import smtplib
import time
from email.message import EmailMessage

import paramiko
import yaml


class Robot:
    def __init__(self, config_path):
        self.has_gpu = self.check_gpus()
        if not self.has_gpu:
            raise Exception('GPU is not available')

        self.func_reflector = {
            "memory": self.memory
        }

        self.config_path = config_path
        self.reload_config(self.config_path)

    def reload_config(self, config_path):
        """
        加载配置文件
        Args:
            config_path: 配置文件路径, 建议使用绝对路径
        """
        with open(config_path, 'rb') as f:
            config = yaml.load(f, yaml.FullLoader)
            self.from_mail = config['FROM_MAIL']
            self.smtp_server = config['SMTP_SERVER']
            self.ssl_port = config['SSL_PORT']
            self.user_pwd = config['USER_PWD']

            self.server_ip = config["SERVER_IP"]
            self.server_port = config["SERVER_PORT"]
            self.server_username = config["SERVER_USERNAME"]
            self.server_passwd = config["SERVER_PASSWD"]

            self.must = config["MUST"]
            self.mem_rate = config["MEM_RATE"]

            self.mail_list = config['MAIL_LIST']
            self.skip_time = config['SKIP_TIME']
            self.trigger_mode = config['TRIGGER_MODE']
            self.lt_mail_cd = config["LT_MAIL_CD"]
            self.et_mail_cd = config["ET_MAIL_CD"]
            self.query_cd = config["QUERY_CD"]
            self.query_func = self.func_reflector[config["QUERY_FUNC"]]

    def check_gpus(self):
        # 建立连接
        trans = paramiko.Transport(("10.0.3.41", 30664))
        trans.connect(username="root", password="1234")
        # 将sshclient的对象的transport指定为以上的trans
        ssh = paramiko.SSHClient()
        ssh._transport = trans
        # 剩下的就和上面一样了
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        stdin, stdout, stderr = ssh.exec_command("nvidia-smi -q")
        out = str(stdout.read(), encoding="utf-8")

        # 关闭连接
        trans.close()

        if not 'NVSMI' in out:  # os.popen('nvidia-smi').read():
            print("'nvidia-smi' tool not found.")
            return False
        return True

    def parse(self, line, qargs):
        """
        Pasing a line of csv format text returned by nvidia-smi
        Args:
            line: a line of text
            qargs: a dict of gpu infos
        Returns:

        """
        numberic_args = ['memory.free', 'memory.total', 'power.draw', 'power.limit', 'temperature.gpu']
        power_manage_enable = lambda v: (not 'Not Support' in v)
        to_numberic = lambda v: float(v.upper().strip().replace('MIB', '').replace('W', ''))
        process = lambda k, v: (
            (int(to_numberic(v)) if power_manage_enable(v) else 1) if k in numberic_args else v.strip())
        return {k: process(k, v) for k, v in zip(qargs, line.strip().split(','))}

    def query_gpu(self):
        qargs = ['index', 'gpu_name', 'memory.free', 'memory.total', 'power.draw', 'power.limit', 'temperature.gpu', 'timestamp']
        cmd = 'nvidia-smi --query-gpu={} --format=csv,noheader'.format(','.join(qargs))

        # 建立连接
        trans = paramiko.Transport((self.server_ip, self.server_port))
        trans.connect(username=self.server_username, password=self.server_passwd)

        ssh = paramiko.SSHClient()
        ssh._transport = trans
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # 执行命令并获取返回
        stdin, stdout, stderr = ssh.exec_command(cmd)
        results = str(stdout.read(), encoding="utf-8").split("\n")[:-1]

        return [self.parse(line, qargs) for line in results]

    def memory(self):
        """
        其他筛选的方式可以根据这个方法实现，如根据power
        Returns:
            返回可用的gpu idx
        """
        gpus = self.query_gpu()

        gpu_idx = []
        for i in range(len(gpus)):
            if gpus[i]['memory.free'] == gpus[i]['memory.total'] or \
                float(gpus[i]['memory.free'] / gpus[i]['memory.total']) >= self.mem_rate:
                gpu_idx.append(i)
        return gpu_idx, json.dumps(gpus, indent=2)

    def send_mail(self, to_mail, title, content):
        """
        发送邮件
        Args:
            to_mail: 接收邮件方
            title: 标题
            content: 正文信息
        Returns:
            发送是否成功, True/False
        """
        try:
            msg = EmailMessage()
            msg['Subject'] = title
            msg['From'] = self.from_mail
            msg['To'] = to_mail
            msg.set_content(content)
            server = smtplib.SMTP_SSL('smtp.qq.com')
            server.login(self.from_mail, self.user_pwd)
            server.send_message(msg)
            server.quit()
            return True
        except Exception as e:
            print(e)
            return False

    def lever_trigger(self, query_func, mail_cd=3600, query_cd=60):
        """
        采用水平触发的方式发送邮件（即间隔固定时间发送邮件）
        Args:
            query_func: 显卡过滤规则函数
            mail_cd: 发送邮件的CD
            query_cd: 调用query_func的频率
        """
        while True:
            time_now = dt.datetime.now()
            # 判断是否在不发送邮件的时间段
            if time_now.hour in self.skip_time:
                time.sleep(3600)
                continue
            # 显存剩余比例
            gpus_list, info = query_func()
            # 打印当前时间
            t = dt.datetime.utcnow()
            t = t + dt.timedelta(hours=8)
            print(f"\n当前时间：{t.strftime('%Y-%m-%d %H:%M:%S')}")
            if len(gpus_list) >= self.must:
                self.notice(gpus_list, info)
                print(f"发送邮件任务完成, 休眠{mail_cd}s")
                time.sleep(mail_cd)
            else:
                print(f"无可用显卡: {info}")
            print(f"进入休眠, 休眠{query_cd}s")
            time.sleep(query_cd)
            print(f"重载配置文件 {self.config_path}")
            self.reload_config(self.config_path)

    def edge_trigger(self, query_func):
        """
        采用边缘触发的方式发送邮件（即当状态发生变化的时候才会发送邮件）
        Args:
            query_func: 显卡过滤规则函数
        """
        gpus_list = []
        while True:
            mail_cd = self.et_mail_cd
            query_cd = self.query_cd
            query_func = self.query_func
            time_now = dt.datetime.now()
            # 判断是否在不发送邮件的时间段
            if time_now.hour in self.skip_time:
                time.sleep(3600)
                continue
            # 显存剩余比例
            tmp_list, info = query_func()
            # 打印当前时间
            t = dt.datetime.utcnow()
            t = t + dt.timedelta(hours=8)
            print(f"\n当前时间：{t.strftime('%Y-%m-%d %H:%M:%S')}")
            # 如果显卡可用情况发生变化
            if gpus_list != tmp_list:
                gpus_list = tmp_list
                # 如果存在空闲的显卡才发送
                if len(gpus_list) >= self.must:
                    self.notice(gpus_list, info)
                    print(f"发送邮件任务完成, 休眠{mail_cd}s")
                    time.sleep(mail_cd)
            else:
                print(f"无可用显卡: {info}")
            print(f"进入休眠, 休眠{query_cd}s")
            time.sleep(query_cd)
            print(f"重载配置文件 {self.config_path}")
            self.reload_config(self.config_path)

    def notice(self, gpus_list, info):
        """
        发送通知邮件
        Args:
            gpus_list: 空闲可用列表
            info: 相关信息
        """
        print(f"显卡空闲可用列表: {gpus_list}, 相关信息：{info}")
        for to in self.mail_list:
            # 隔5秒发一个人,防止被当成垃圾邮件
            time.sleep(5)
            send = self.send_mail(to, f"显卡空闲可用列表: {gpus_list}", f"服务器地址：{self.server_ip}:{self.server_port}\n 相关信息：{info}")
            if send:
                print(f"发送邮件至{to}成功")
            else:
                print(f"发送邮件至{to}失败！")

    def run(self):
        if self.trigger_mode == 'LT':
            self.lever_trigger(self.query_func, self.lt_mail_cd, self.query_cd)
        else:
            self.edge_trigger(self.query_func)


if __name__ == "__main__":
    config_path = "config.yml"
    robot = Robot(config_path)
    robot.run()
