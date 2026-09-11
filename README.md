# USB Control Master — Centralized USB + MTP/WPD Policy Server

Server untuk mengontrol **USB Mass Storage** dan **MTP/WPD** secara terpusat
via dashboard web. Agent Windows di client akan polling policy tiap 10 detik.

## Arsitektur

```
┌─────────────────────┐       ┌──────────────────────┐
│   VPS (Linux)       │       │  Client (Windows)     │
│   ┌─────────────┐   │ HTTP  │  ┌────────────────┐   │
│   │ Master      │◄──┼───────┼──│ Agent Service  │   │
│   │ Port 1122   │   │       │  │ USBControl.exe │   │
│   │ FastAPI     │   │       │  └────────────────┘   │
│   │ SQLite      │   │       │        │               │
│   └─────────────┘   │       │        ▼               │
│         │           │       │  ┌────────────────┐    │
│    nginx reverse     │       │  │ Registry       │   │
│    proxy (443/80)    │       │  │ USBSTOR Start  │   │
│         │           │       │  └────────────────┘    │
│    usbcontrol.       │       │        │               │
│    qwiz.web.id       │       │        ▼               │
│                      │       │  ┌────────────────┐    │
│                      │       │  │ pnputil        │   │
│                      │       │  │ MTP/WPD on/off │   │
│                      │       │  └────────────────┘    │
└─────────────────────┘       └──────────────────────┘
```

## Server (VPS) — SUDAH TERINSTAL

| Komponen | Detail |
|---|---|
| Backend | FastAPI di port 1122 (systemd service) |
| Reverse Proxy | nginx — usbcontrol.qwiz.web.id |
| SSL | Belum (tunggu DNS di-set) |
| Database | `master.db` (SQLite) — auto-init |
| Dashboard | https://usbcontrol.qwiz.web.id (setelah SSL) |

### Service Management

```bash
# Status
systemctl status usbcontrol-master

# Restart
systemctl restart usbcontrol-master

# Logs
journalctl -u usbcontrol-master -f
```

## Agent (Client Windows) — Didistribusikan ke User

File untuk client Windows ada di:
```
/root/USBControlMaster/dist/USB_Control_Agent_Source.zip
```

## Cara Distribusi — Tinggal Download + Jalanin

Ada 2 cara:

### Cara A: Download EXE langsung (termudah — RECOMMENDED)

1. **Download** `USBControlAgent.exe` dari [GitHub Releases](https://github.com/rizkykhai/USBControlMaster/releases)
2. **Klik kanan** → **Run as Administrator**
3. Masukkan Master URL dan Registration Key → selesai, otomatis jalan sebagai service

EXE di-build otomatis oleh GitHub Actions setiap ada update kode. Tidak perlu install Python atau build manual.

### Cara B: Build sendiri dari source
1. **Ekstrak zip** di PC Windows dengan Python terinstall
2. **Klik kanan** `build_exe.bat` -> **Run as Administrator**
3. Hasil build: `dist/USBControlAgent.exe`
4. **Copy EXE ke komputer client**, jalankan sebagai Administrator
5. Masukkan Master URL dan Registration Key

Atau lihat README lengkap di dalam zip.
