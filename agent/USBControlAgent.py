import ctypes
import getpass
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path

try:
    import certifi
except ImportError:
    certifi = None  # fallback: use default SSL context

# Required by pywin32 services, especially when frozen by PyInstaller.
import win32timezone  # noqa: F401
import servicemanager
import win32event
import win32service
import win32serviceutil

APP_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "USBControlAgent"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "agent.log"
SERVICE_NAME = "USBControlAgent"
SERVICE_DISPLAY = "USB Control Agent"
POLL_SECONDS = 10


def log(message):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_admin():
    params = subprocess.list2cmdline(sys.argv[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return rc > 32


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def stable_device_id():
    old = load_state().get("device_id")
    if old:
        return old
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        # Keep the same deterministic style across reinstalls on this machine.
        return hashlib.sha256(str(guid).encode("utf-8")).hexdigest().upper()[:12]
    except Exception:
        return hashlib.sha256(socket.gethostname().upper().encode("utf-8")).hexdigest().upper()[:12]


def http_json(url, method="GET", data=None, token=None):
    body = None
    headers = {"User-Agent": "USBControlAgent/FinalV8"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    ctx = None
    if url.startswith("https://") and certifi is not None:
        ctx = ssl.create_default_context(cafile=certifi.where())

    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.URLError as e:
        if "certificate verify failed" in str(e):
            log(f"SSL ERROR: {e}")
            raise RuntimeError(
                "Sertifikat SSL server tidak dapat diverifikasi. "
                "Coba: 1) Sync jam/date PC, 2) Update Windows, "
                "3) Update Python & certifi (pip install -U certifi). "
                f"Detail: {e}"
            )
        raise


def set_usb_storage(enabled, state=None):
    # USB Mass Storage: 3 = enabled, 4 = disabled.
    value = 3 if bool(enabled) else 4
    reg_path = r"HKLM\SYSTEM\CurrentControlSet\Services\USBSTOR"

    result = subprocess.run(
        ["reg", "add", reg_path, "/v", "Start", "/t", "REG_DWORD",
         "/d", str(value), "/f"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout).strip() or "Registry update failed"
        )

    # Verify that Windows actually accepted the requested value.
    verify = subprocess.run(
        ["reg", "query", reg_path, "/v", "Start"],
        capture_output=True, text=True
    )
    if verify.returncode != 0:
        raise RuntimeError(
            (verify.stderr or verify.stdout).strip() or "Registry verification failed"
        )

    expected = f"0x{value:x}"
    if expected not in (verify.stdout or "").lower():
        raise RuntimeError(
            f"USBSTOR verification failed: expected Start={expected}; "
            f"got: {(verify.stdout or '').strip()}"
        )

    log(f"USBSTOR applied and verified: Start={expected}")

    # Also enable/disable present USB mass storage devices via pnputil,
    # so already-connected USBs are affected immediately (not just new ones).
    state = state if isinstance(state, dict) else {}
    disabled_ids = set(state.get("usb_disabled_instance_ids", []))
    ps = r"""
$devices = @(Get-PnpDevice | Where-Object { $_.InstanceId -like "*USBSTOR*" })
foreach ($d in $devices) {
  [PSCustomObject]@{Status=[string]$d.Status; FriendlyName=[string]$d.FriendlyName; InstanceId=[string]$d.InstanceId} | ConvertTo-Json -Compress
}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        log(f"USB storage enumeration error: {(result.stderr or result.stdout).strip()}")
        return

    devices = []
    for line in (result.stdout or "").splitlines():
        try:
            obj = json.loads(line.strip())
            if obj.get("InstanceId"):
                devices.append(obj)
        except Exception:
            pass

    if enabled:
        # Re-enable previously disabled USB storage devices.
        targets = set(disabled_ids)
        targets.update(d["InstanceId"] for d in devices
                       if str(d.get("Status", "")).lower() == "disabled")
        for iid in sorted(targets):
            r = subprocess.run(
                ["pnputil.exe", "/enable-device", iid],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode != 0:
                log(f"Failed to enable USB storage device {iid}: {(r.stderr or r.stdout).strip()}")
        state["usb_disabled_instance_ids"] = []
        log(f"USB storage ENABLED: re-enabled {len(targets)} device instance(s)")
    else:
        present = [d for d in devices
                   if str(d.get("Status", "")).lower() == "ok"]
        new_ids = []
        failures = []
        for d in present:
            iid = d["InstanceId"]
            r = subprocess.run(
                ["pnputil.exe", "/disable-device", iid],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode == 0:
                new_ids.append(iid)
            else:
                failures.append((iid, (r.stderr or r.stdout).strip()))
        if failures and not new_ids:
            detail = "; ".join(f"{iid}: {msg}" for iid, msg in failures)
            raise RuntimeError(f"USB storage disable failed: {detail}")
        state["usb_disabled_instance_ids"] = new_ids
        log(f"USB storage DISABLED: disabled {len(new_ids)} present device instance(s) via pnputil")


def set_mtp(enabled, state=None):
    """Enable/disable present Windows Portable Devices (WPD/MTP).
    Disabled instance IDs are cached so the same devices can be re-enabled.
    """
    state = state if isinstance(state, dict) else {}
    disabled_ids = set(state.get("mtp_disabled_instance_ids", []))
    ps = r"""
$ErrorActionPreference = 'Stop'
$devices = @(Get-PnpDevice -Class WPD)
foreach ($d in $devices) {
  [PSCustomObject]@{Status=[string]$d.Status; FriendlyName=[string]$d.FriendlyName; InstanceId=[string]$d.InstanceId} | ConvertTo-Json -Compress
}
"""
    result = subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-Command",ps], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "WPD enumeration failed")
    devices=[]
    for line in (result.stdout or "").splitlines():
        try:
            obj=json.loads(line.strip())
            if obj.get("InstanceId"): devices.append(obj)
        except Exception: pass
    if enabled:
        targets=set(disabled_ids)
        targets.update(d["InstanceId"] for d in devices if str(d.get("Status","")).lower()=="disabled")
        for iid in sorted(targets):
            r=subprocess.run(["pnputil.exe","/enable-device",iid],capture_output=True,text=True,timeout=30)
            if r.returncode!=0:
                raise RuntimeError((r.stderr or r.stdout).strip() or f"Failed to enable WPD device: {iid}")
        state["mtp_disabled_instance_ids"]=[]
        log(f"MTP/WPD ENABLED: re-enabled {len(targets)} device instance(s)")
    else:
        present=[d for d in devices if str(d.get("Status","")).lower()=="ok"]
        new_ids=[]; failures=[]
        for d in present:
            iid=d["InstanceId"]
            r=subprocess.run(["pnputil.exe","/disable-device",iid],capture_output=True,text=True,timeout=30)
            if r.returncode==0: new_ids.append(iid)
            else: failures.append((iid,(r.stderr or r.stdout).strip()))
        if failures:
            details="; ".join(f"{iid}: {msg}" for iid,msg in failures)
            raise RuntimeError(f"WPD/MTP disable failed: {details}")
        state["mtp_disabled_instance_ids"]=new_ids
        log(f"MTP/WPD DISABLED: disabled {len(new_ids)} present device instance(s)")


def service_exists():
    result = subprocess.run(
        ["sc.exe", "query", SERVICE_NAME],
        capture_output=True, text=True
    )
    return result.returncode == 0 and SERVICE_NAME in (result.stdout or "")


def service_running():
    result = subprocess.run(
        ["sc.exe", "query", SERVICE_NAME],
        capture_output=True, text=True
    )
    return "RUNNING" in (result.stdout or "")


def install_and_start_service():
    exe = os.path.abspath(sys.executable)
    if not exe.lower().endswith(".exe"):
        raise RuntimeError("Service installation must run from the EXE.")

    # Always make the Windows Service point to THIS EXE. This matters when V3
    # is extracted into a new folder and the old service registration still
    # points to a deleted V2 EXE.
    if service_exists():
        subprocess.run([exe, "stop"], capture_output=True, text=True)
        time.sleep(1)
        old = subprocess.run([exe, "remove"], capture_output=True, text=True)
        combined = (old.stdout or "") + (old.stderr or "")
        if old.returncode != 0 and "1060" not in combined and "does not exist" not in combined.lower():
            raise RuntimeError(combined.strip() or "Could not remove old service")

    result = subprocess.run(
        [exe, "install", "--startup", "auto"],
        capture_output=True, text=True
    )
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(combined.strip() or "Service installation failed")

    result = subprocess.run([exe, "start"], capture_output=True, text=True)
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and not service_running():
        raise RuntimeError(combined.strip() or "Service start failed")


def enroll():
    if not is_admin():
        print("Meminta hak Administrator...")
        if not relaunch_admin():
            print("Gagal meminta hak Administrator.")
            input("Tekan Enter untuk keluar...")
        return

    state = load_state()
    print("=" * 55)
    print(" USB CONTROL AGENT - FIRST TIME REGISTRATION")
    print("=" * 55)
    print(f"Device      : {socket.gethostname()}")
    print(f"User        : {getpass.getuser()}")
    did = stable_device_id()
    print(f"Device ID   : {did}")

    default_master = state.get("master_url", "http://10.255.255.59:8000")
    master = input(f"Master URL [{default_master}]: ").strip() or default_master
    master = master.rstrip("/")
    key = input("Registration Key: ").strip()

    if not key:
        print("Registration Key wajib diisi.")
        input("Tekan Enter untuk keluar...")
        return

    try:
        result = http_json(
            master + "/api/agent/enroll",
            method="POST",
            data={"key": key, "device_id": did, "device_name": socket.gethostname()}
        )
        token = result.get("agent_token") or result.get("token")
        if not token:
            raise RuntimeError(result.get("detail") or "Master tidak mengembalikan token.")

        save_state({
            "master_url": master,
            "device_id": did,
            "device_name": socket.gethostname(),
            "username": getpass.getuser(),
            "agent_token": token,
            "usb_enabled": bool(result.get("usb_enabled", True)),
            "mtp_enabled": bool(result.get("mtp_enabled", True)),
        })
        print("\nEnrollment BERHASIL.")
        print(f"Device ID : {did}")
        print("Credential tersimpan.")
        print("\nMenginstall Windows Service...")
        install_and_start_service()
        print("Windows Service berhasil di-install/dijalankan.")
        print("\nSetup selesai. Kontrol USB dilakukan dari Master.")
    except urllib.error.HTTPError as e:
        detail = ""
        try: detail = e.read().decode("utf-8")
        except Exception: pass
        print(f"\nEnrollment GAGAL. HTTP {e.code}: {detail}")
    except Exception as e:
        print(f"\nSetup GAGAL: {e}")
    input("\nTekan Enter untuk keluar...")


def run_agent():
    if not is_admin():
        log("ERROR: Agent must run as Administrator/LocalSystem.")
        return

    state = load_state()
    master_url = state.get("master_url")
    token = state.get("agent_token")
    device_id = state.get("device_id")
    if not master_url or not token or not device_id:
        log("ERROR: Enrollment data missing.")
        return

    last_policy = bool(state.get("usb_enabled", True))
    last_mtp_policy = bool(state.get("mtp_enabled", True))

    # Try to fetch the latest policy from server FIRST before applying cached.
    # This avoids re-applying a stale cached policy after a long outage.
    try:
        data = http_json(master_url + "/api/agent/policy", token=token)
        last_policy = bool(data.get("usb_enabled", last_policy))
        last_mtp_policy = bool(data.get("mtp_enabled", last_mtp_policy))
        log(f"Startup: fetched latest policy from server — USB={'ENABLED' if last_policy else 'DISABLED'}")
    except Exception as e:
        log(f"Startup: cannot reach server ({e}), applying cached policy.")

    # Apply current policy on service start.
    try:
        set_usb_storage(last_policy, state)
        log(f"Startup USB policy applied: {'ENABLED' if last_policy else 'DISABLED'}")
    except Exception as e:
        log(f"Initial USB policy error: {e}")

    try:
        set_mtp(last_mtp_policy, state)
        log(f"Startup MTP/WPD policy applied: {'ENABLED' if last_mtp_policy else 'DISABLED'}")
    except Exception as e:
        log(f"Initial MTP/WPD policy error: {e}")

    log(f"Agent started. Device={device_id}; USB={'ENABLED' if last_policy else 'DISABLED'}")

    consecutive_errors = 0

    while True:
        try:
            data = http_json(master_url + "/api/agent/policy", token=token)
            enabled = bool(data.get("usb_enabled", last_policy))
            mtp_enabled = bool(data.get("mtp_enabled", last_mtp_policy))

            set_usb_storage(enabled, state)
            set_mtp(mtp_enabled, state)

            if enabled != last_policy:
                log(f"Master USB policy changed: {'ENABLED' if enabled else 'DISABLED'}")
            if mtp_enabled != last_mtp_policy:
                log(f"Master MTP policy changed: {'ENABLED' if mtp_enabled else 'DISABLED'}")

            last_policy = enabled
            last_mtp_policy = mtp_enabled
            state["usb_enabled"] = enabled
            state["mtp_enabled"] = mtp_enabled
            state["policy_applied_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_state(state)
            consecutive_errors = 0

        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors == 1:
                log(f"WARNING: Gagal hubungi server ({e}). Policy terakhir tetap berlaku. Akan coba lagi setiap {POLL_SECONDS}s.")
            elif consecutive_errors % 6 == 0:  # log every ~1 minute of failures
                log(f"WARNING: Masih gagal hubungi server setelah {consecutive_errors * POLL_SECONDS}s. Error: {e}")

        time.sleep(POLL_SECONDS)


class USBControlAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = "Centralized USB Mass Storage policy agent."

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        # Report RUNNING immediately so Windows SCM does not time out while
        # network/registry initialization is performed.
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        servicemanager.LogInfoMsg("USBControlAgent service started.")
        worker = threading.Thread(target=run_agent, name="USBControlAgentWorker", daemon=True)
        worker.start()

        # Keep the service alive until Windows requests a stop.
        while True:
            rc = win32event.WaitForSingleObject(self.hWaitStop, 1000)
            if rc == win32event.WAIT_OBJECT_0:
                break

        self.ReportServiceStatus(win32service.SERVICE_STOPPED)


def run_as_windows_service():
    # Explicitly enter the pywin32 service dispatcher when Windows SCM
    # launches this EXE with no custom arguments. This avoids relying on
    # HandleCommandLine's argument parser in a frozen one-file executable.
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(USBControlAgentService)
    servicemanager.StartServiceCtrlDispatcher()


if __name__ == "__main__":
    # Windows SCM launches the service EXE with no custom arguments.
    # Enrollment is explicit via: USBControlAgent.exe --enroll
    if len(sys.argv) == 1:
        run_as_windows_service()
    elif sys.argv[1].lower() == "--enroll":
        enroll()
    else:
        win32serviceutil.HandleCommandLine(USBControlAgentService)
