module.exports = {
  apps: [{
    name: 'usbcontrol-master',
    cwd: '/root/USBControlMaster/master',
    script: 'venv/bin/python',
    args: ['-m', 'uvicorn', 'server:app', '--host', '0.0.0.0', '--port', '1122', '--workers', '1'],
    interpreter: 'none',
    env: {
      PATH: '/root/USBControlMaster/master/venv/bin:/usr/bin:/bin'
    }
  }]
};
