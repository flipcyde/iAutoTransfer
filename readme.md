# 📸 iAutoTransfer — Apple Stack (AFC Edition)

**iAutoTransfer** is a cross-platform Python GUI app for scanning and transferring photos & videos from iPhones using Apple’s **AFC (Apple File Conduit)** protocol via [`pymobiledevice3`](https://github.com/doronz88/pymobiledevice3).

Built for high-speed parallel file transfers, live worker telemetry, and optional HEIC→JPEG conversion — all without iTunes or iCloud.

---

## 🌟 Features

✅ **AFC/DCIM Scanning**
- Fast recursive scan of `/DCIM` via a single AFC session  
- Filters by **year**, **month**, and **media type** (photo/video)  

✅ **Parallel File Transfer**
- Multi-threaded AFC sessions with live per-worker stats  
- Pause / Resume / Stop controls  
- Optional HEIC→JPEG conversion (using Pillow-HEIF)  
- Optional flatten output and manifest CSV logging  

✅ **Live Telemetry Dashboard**
- Gradient progress bar with glow animation  
- Worker table showing ID, status, files processed, Mbps, and last file  
- Real-time throughput (files/s, MB/s) and ETA  
- Storage + battery info read from Lockdown  

✅ **Dark Themed Tkinter UI**
- Modern dark styling for all widgets  
- Smooth progress animations  
- Compact layout optimized for Windows 11  

---

## 🖼️ Preview

<p align="center">
  <img src="docs/screenshot_ui.png" width="800">
</p>

---

## 🧩 Requirements

### ✅ System
- Windows 10/11 (64-bit)
- Python 3.12+
- iPhone/iPad **unlocked** and **Trusted** on this computer
- **Apple Mobile Device Support** and **Apple Application Support (64-bit)**  
  (installed automatically with iTunes, or from Apple’s standalone driver packages)

### 🧰 Python Libraries
All dependencies are defined in `requirements.txt`:

```bash
pymobiledevice3==4.4.0
pillow==11.0.0
pillow-heif==0.17.0
tqdm==4.66.5
requests==2.32.3
