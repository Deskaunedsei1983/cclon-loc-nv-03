# Host-Wartung: nach NVIDIA-Treiber-/Kernel-Update (WICHTIG)

**Gilt für den GPU-Host** (CachyOS/Arch: `nvidia-open-dkms` + `nvidia-container-toolkit` + Docker).
Diese Schritte sind ein **Host-Eingriff, NICHT in git** — darum dieses Runbook als Gedächtnisstütze.

> Faustregel: **Immer wenn `pacman -Syu` ein `nvidia-*`- oder `linux-*`-Paket aktualisiert,
> danach diese TODO-Liste abarbeiten.** Sonst starten die GPU-Container mit „CUDA unknown error".

---

## ✅ TODO nach jedem Treiber-/Kernel-Update

```fish
# 1) Reboot — lädt das zum neuen Treiber passende Kernelmodul
#    (nötig, sobald linux-cachyos* ODER nvidia-* aktualisiert wurde)
sudo reboot

# 2) CDI-Spec für den NEUEN Treiber neu erzeugen + Docker neu starten
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
sudo systemctl restart docker

# 3) GPU IM CONTAINER verifizieren (muss "avail: True" + Gerätename zeigen)
set IMG (grep '^VLLM_IMAGE=' .env | cut -d= -f2)
docker run --rm --gpus all --entrypoint python3 $IMG \
  -c "import torch; print('avail:', torch.cuda.is_available()); print(torch.cuda.get_device_name(0))"

# 4) Stack hochfahren
docker compose up -d
```

Zeigt Schritt 3 `avail: True` + `NVIDIA RTX PRO 6000 ...` → fertig. Wenn nicht → Abschnitt „Diagnose".

---

## Symptom (so erkennst du genau diesen Fall)

vLLM-Container crasht beim Start:

```
Failed to get device capability: CUDA unknown error ... Setting the available devices to be zero.
RuntimeError: CUDA unknown error ...   (in torch._C._cuda_init())
```

…**aber auf dem Host läuft `nvidia-smi` normal** und zeigt die GPU. Dieser Fingerabdruck
(**Host-GPU ok, Container-CUDA tot**) = die nvidia-container-toolkit/CDI-Schicht passt nicht
mehr zum neuen Treiber.

## Warum

`pacman -Syu` zieht oft **Treiber** (`nvidia-open-dkms`/`nvidia-utils`) und/oder **Kernel**
(`linux-cachyos`) mit. Zwei Dinge brechen dann für Container:

1. **Kernelmodul-Mismatch** (Treiber-Userspace ≠ laufendes Kernelmodul) → **Reboot** behebt es.
2. **Veraltete CDI-Spec**: `/etc/cdi/nvidia.yaml` pinnt exakte Treiber-Library-Pfade/-Versionen
   (`libcuda.so.<version>`, `libnvidia-ml.so.<version>`, …). Nach dem Versionssprung zeigen sie
   auf nicht mehr existente Dateien → der Container bekommt kaputte/fehlende CUDA-Libs →
   „CUDA unknown error". `nvidia-ctk cdi generate` schreibt die Spec für die neue Treiberversion
   neu; `systemctl restart docker` lädt sie.

Merkhilfe: **NVML (`nvidia-smi`) kann im Container funktionieren, obwohl die CUDA-Runtime tot ist** —
nvidia-smi liest nur GPU-Infos, CUDA-Init erzeugt einen echten Compute-Context und ist strenger.

---

## Diagnose (falls die TODO-Liste nicht reicht)

```fish
set IMG (grep '^VLLM_IMAGE=' .env | cut -d= -f2)
echo "### Host-GPU (muss gehen)"; nvidia-smi | head -12
echo "### Treiber Kernel-Sicht vs installiert"; cat /proc/driver/nvidia/version; pacman -Q nvidia-utils nvidia-open-dkms
echo "### laufender Kernel + seit wann up (rebootet?)"; uname -r; uptime
echo "### Module (uvm dabei?) + Device-Nodes"; lsmod | grep '^nvidia'; ls -l /dev/nvidia*
echo "### Container NVML"; docker run --rm --gpus all --entrypoint nvidia-smi $IMG | head -12
echo "### Container CUDA-Runtime (der eigentliche Test)"; docker run --rm --gpus all --entrypoint python3 $IMG -c "import torch; print('avail:', torch.cuda.is_available())"
echo "### Arch-Falle: ldconfig-Pfad im Toolkit"; grep -nE 'ldconfig|no-cgroups' /etc/nvidia-container-runtime/config.toml
```

| Beobachtung | Ursache | Fix |
|---|---|---|
| Host `nvidia-smi` kaputt (`NVML: Driver/library version mismatch`) | Kernelmodul ≠ Treiber | **Reboot** |
| Host ok, Container-NVML ok, **Container-CUDA `False`** | CDI-Spec veraltet | `nvidia-ctk cdi generate …` + `systemctl restart docker` |
| `nvidia_uvm` fehlt in `lsmod` / `/dev/nvidia-uvm` fehlt | UVM-Modul nicht geladen | `sudo modprobe nvidia_uvm && sudo nvidia-modprobe -u -c=0` |
| Alles da, Container-CUDA trotzdem `False` | **Arch-Falle** | in `/etc/nvidia-container-runtime/config.toml` `ldconfig = "@/usr/bin/ldconfig"` setzen (nicht Debian-Default `/sbin/ldconfig.real`), dann `systemctl restart docker` |

---

## Notizen

- `--gpus all` **und** die Compose-`deploy.resources.reservations.devices` laufen beide über die
  nvidia-Runtime/CDI — beide brauchen die **frische** CDI-Spec nach einem Treiber-Update.
- **Test-Kommandos brauchen `--entrypoint`**: das vLLM-Image hat `vllm serve` als Entrypoint,
  d. h. `docker run … <img> nvidia-smi` interpretiert `nvidia-smi` als **Modellnamen** (HF-404).
  Darum `--entrypoint nvidia-smi` bzw. `--entrypoint python3`. Und das Binary heißt **`python3`**,
  nicht `python`.
- `nvidia-smi` im Container darf keine Prozesse listen, solange vLLM nicht läuft — das ist normal.
