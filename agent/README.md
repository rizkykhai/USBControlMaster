# USB Control Agent — Panduan Build & Install

Agent Windows untuk kontrol terpusat USB Mass Storage dan MTP/WPD.
Bekerja bersama **Master Server** yang sudah berjalan di VPS.

## Persyaratan (Komputer Build)

- Windows 10 / 11
- Python 3.8+ sudah terinstall (https://python.org)
- Pip sudah terinstall (biasanya otomatis bersama Python)
- Koneksi internet (untuk download dependencies)

## Cara Build EXE

### Cara Cepat (double-click)

1. Buka folder `agent/`
2. **Klik kanan** `build_exe.bat` -> **Run as Administrator**
3. Tunggu sampai selesai (1-5 menit tergantung koneksi)
4. Hasil: `agent/dist/USBControlAgent.exe`

### Cara Manual (Command Prompt)

```cmd
cd C:\path\ke\agent
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --clean --onefile --name USBControlAgent --hidden-import=win32timezone USBControlAgent.py
```

## Cara Install di Komputer Client

### 1. Persiapan dari Master Dashboard

Buka dashboard master (https://usbcontrol.qwiz.web.id):
- Klik tombol **"+ Registration Key"**
- Copy key yang muncul (contoh: `A1B2-C3D4-E5F6-G7H8`)

### 2. Install Agent di PC Client

1. Copy `USBControlAgent.exe` ke PC client
2. **Klik kanan** `USBControlAgent.exe` -> **Run as Administrator**
3. Akan muncul jendela enrollment:

```
=======================================================
 USB CONTROL AGENT - FIRST TIME REGISTRATION
=======================================================
Device      : KOMPUTER-USER
User        : user
Device ID   : A1B2C3D4E5F6
Master URL [http://10.255.255.59:8000]:
```

4. Ketik **Master URL**:
   ```
   https://usbcontrol.qwiz.web.id
   ```
5. Masukkan **Registration Key** (yang dibuat dari dashboard)
6. Enter. Jika berhasil akan tampil:
   - `Enrollment BERHASIL`
   - `Windows Service berhasil di-install/dijalankan`

### 3. Verifikasi

- Buka dashboard https://usbcontrol.qwiz.web.id
- Device baru akan muncul di tabel
- Status: **ENABLED** untuk USB dan MTP

## Yang Bisa Dikontrol dari Dashboard

| Fitur | Fungsi |
|---|---|
| **USB Mass Storage** | Enable/disable akses flashdisk, harddisk eksternal |
| **MTP / WPD** | Enable/disable akses HP Android via kabel USB |
| **Rename** | Mengubah nama tampilan device |

## Troubleshooting

**Enrollment Gagal?**
- Pastikan EXE di-**Run as Administrator**
- Pastikan Master URL benar (https://usbcontrol.qwiz.web.id)
- Pastikan Registration Key masih valid (belum dipakai)

**Service Tidak Jalan?**
- Buka `services.msc`
- Cari service **USB Control Agent**
- Klik kanan -> Start

**Log File:**
```
C:\ProgramData\USBControlAgent\agent.log
```
