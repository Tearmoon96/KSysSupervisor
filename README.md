# KSysSupervisor

A hardware monitor for Linux. Temperatures, fan speeds, clocks, voltages,
power draw and load for every device in the machine, each with its minimum and
maximum since the app started. Very similar to HWmonitor for windows.

It also does manual fan control and has a stress test with live graphs

<p>
  <img src="https://gist.githubusercontent.com/Tearmoon96/9b04efaf9cb4539e61fc90158f12fd36/raw/tree-layout.png" alt="Tree layout" width="34%">
    <img src="https://gist.githubusercontent.com/Tearmoon96/9b04efaf9cb4539e61fc90158f12fd36/raw/tile-layout.png" alt="Tile layout" width="64%">
</p>

![Stress test with live graphs](https://gist.githubusercontent.com/Tearmoon96/9b04efaf9cb4539e61fc90158f12fd36/raw/stress-test-graphs.png)

## Installing

Each [release](https://github.com/Tearmoon96/KSysSupervisor/releases) comes in
two versions. They're the same program.

**AppImage.** One file with its own Python, PyQt6 and psutil inside, so there's
nothing to install:

```sh
chmod +x KSysSupervisor-*-x86_64.AppImage
./KSysSupervisor-*-x86_64.AppImage
```

**Python version** (`KSysSupervisor-<version>.tar.gz`). You can run directly the python file, otherwise it comes with an install
script. It uses your distribution's PyQt6 and psutil if you have them, and sets
up a private virtual environment with them if you don't:

```sh
tar xf KSysSupervisor-*.tar.gz
cd KSysSupervisor-*/
./install.sh           # just for you, in ~/.local/share/ksyssupervisor
sudo ./install.sh      # for everyone, in /opt/ksyssupervisor
```

Either way you get a `ksyssupervisor` command and an entry in the application
menu. The system-wide install also sets up the fan control helper; with a
per-user install the app offers to do that the first time you open Fan
Control.

To remove it, run the uninstaller from the same folder (there's also a copy in
the install directory):

```sh
./uninstall.sh                      # removes it, asks for sudo if it needs to
./uninstall.sh --purge              # same, plus your settings
sudo ./uninstall.sh --fan-control   # only the fan control helper
```

`--help` on either script shows the other options.

### Updates

A few seconds after it starts, KSysSupervisor asks GitHub whether there's a
newer release. If there is, it downloads it, checks it against the release's
SHA-256 checksums, replaces the old version and restarts. Your settings, tile
layouts and names are kept. If it was installed for all users you'll be asked
for your password; otherwise you won't notice much.

If you'd rather be in charge of that, it's all in the **Help** menu: untick
**Install Updates Automatically** to be asked first, or **Check for Updates at
Startup** to stop it checking at all. **Check for Updates...** checks right
away. When you run it straight from a git clone it only tells you a new version
is out, and `git pull` is up to you.

## What it reads

| Device | Readings |
|---|---|
| CPU | Package and per-core temperatures, clocks, load, power (RAPL) |
| GPU | amdgpu, radeon, nouveau, i915, xe and NVIDIA: temperatures, clocks, fan, power, VRAM. All the cards, not just the first one |
| Memory | Usage, and per-DIMM temperatures with `spd5118` (DDR5) or `jc42` (DDR3/DDR4) |
| Drives | NVMe, and SATA/SAS disks through `drivetemp`, named by model, plus how full each mounted volume is |
| Motherboard | Super-I/O fans, temperatures and voltage rails, and any other chip the kernel exposes |
| Battery | Charge, health compared to design capacity, voltage, power, temperature |

With lm_sensors installed it reads `sensors -j`, otherwise it uses
`/sys/class/hwmon`. It works either way, lm_sensors just labels a few more
things.

Some sensors need a kernel module that distributions don't load by default.
When that's the case, **Help → Hardware Setup** tells you which one, what you'd
gain, and the commands to load it (and keep it loaded after a reboot). It
never runs them for you.

## Tree or tiles

There are two layouts and **View → Layout** switches between them.

The **tree** is the classic HWMonitor look: one branch per device, one row per
reading, with value, min and max. Right-click a device to rename it, drag the
handle on the left to reorder, and hide whole groups from **View →
Categories**.

The **tiles** give each device a card and put the numbers you'd actually look
at first on top: rings for things with a real maximum (load, memory, disk
space) and small chips for the rest. Everything else is listed underneath, and
long lists like sixteen core clocks are folded under **More readings**. The
grid goes from one to four columns depending on how wide the window is.

Each kind of device has its own muted colour so you can find the GPU without
reading titles. Heat gets its own colours on top of that: past 80 °C a reading
and its card turn amber, past 90 °C red, and the card's header says WARM or
HOT. That happens even if the hot reading is folded away.

To customize the tile click the pencil next to a tile's name and the
tile turns into its own editor: every reading can be a chip, a row
below, tucked into More readings, or hidden. You can reorder them, rename them
by clicking on them, and pick a different colour for the tile. **Defaults**
puts it all back. The four-way arrow in the corner drags the tile somewhere
else on the board.

Names, order, tile layouts and window sizes are all remembered, and both
layouts share the same names.

## Fan control

**Tools → Fan Control** lets you set fan speeds by hand. This is the only part
of KSysSupervisor that changes anything on your system; everything else only
reads.

![Fan control](https://gist.githubusercontent.com/Tearmoon96/9b04efaf9cb4539e61fc90158f12fd36/raw/fan-control.png)

Putting a fan on Manual turns off your board's own regulation for that header,
so giving control back is the default and not something you have to remember.
The writing is done by a small helper that runs as root (you authorise it
through polkit), and when it exits it puts every fan it touched back the way it
found it. That includes KSysSupervisor crashing or being killed: the kernel
closes the pipe and the helper takes that as its cue. Fans only stay at your
settings if you explicitly ask for that when closing the window.

Fans are grouped: the graphics card's first, then the headers the board says
cool the CPU, then the rest. The app reads what each driver says (which value
means manual, what range a channel takes, which headers follow the CPU
temperature) instead of guessing, and if a fan can't be controlled it tells
you why.

One oddity: 100% on the slider is 98% of the fan actual capacity. I ran into some 
problems when fans were literally at 100% so i opted for making 98% the new 100%. 
The raw PWM value next to each fan is always the real one.

The helper has to be installed as root, otherwise there'd be no point in
authorising it. The app notices when it's missing or out of date and offers to
install it, which is one password prompt. From a terminal:

```sh
./KSysSupervisor-*-x86_64.AppImage --install-fan-helper   # AppImage
ksyssupervisor --install-fan-helper                      # installed with install.sh
python3 KSysSupervisor.py --install-fan-helper           # source folder
```

If you'd rather do it by hand from the source folder:

```sh
sudo install -Dm755 helpers/ksyssupervisor-fanhelper \
    /usr/local/lib/ksyssupervisor/ksyssupervisor-fanhelper
sed 's|@HELPER_PATH@|/usr/local/lib/ksyssupervisor/ksyssupervisor-fanhelper|' \
    packaging/org.ksyssupervisor.fancontrol.policy \
    | sudo tee /usr/share/polkit-1/actions/org.ksyssupervisor.fancontrol.policy
```

To remove it: `sudo ./uninstall.sh --fan-control`, or
`sudo rm -r /usr/local/lib/ksyssupervisor /usr/share/polkit-1/actions/org.ksyssupervisor.fancontrol.policy`.

## Stress test

**Tools → Stress Test** loads the CPU, the memory or GPU.

You pick how many CPU workers to run and whether they stay on physical cores,
how much RAM to fill, and how much graphics memory. Every load also takes a
percentage, so you can hold things at 40% instead of flat out. That's handy
for finding the point where the fans get loud, or for a long soak test.

The GPU has a few workloads, because which part of a card gives up first
depends on what you ask it to do:

- **Transcendental** (the default): a long chain of sines and square roots.
- **Arithmetic throughput**: lots of independent multiply-adds, keeps the
  shader cores full.
- **Memory bandwidth**: reads all over a big texture, so the card waits on its
  VRAM. This heats the memory and its controllers, which have limits of their
  own.
- **Combined**: alternates the last two, which is closer to what a game does.

They really are different. On my RX 6800 the two arithmetic ones both sit at
the ~202 W board power limit, while the memory one shows the same utilisation
at ~181 W and runs a few degrees cooler.

Before you start, you choose what happens if it gets hot: stop automatically
above a temperature you set, or just warn you and keep going.

> **A stress test at high temperatures can permanently damage your hardware.**
> If you run it without a limit you do so at your own risk. I take no
> responsibility for any damage.

When the test starts, live graphs of the parts under load open next to it.
They go back a few minutes before the start, so you see the climb from idle.
Each load runs in its own process, so stopping the test, closing the app or
killing it ends the load and gives the memory back.


## Live graphs

**Tools → Live Graphs** opens a panel on the right of the main window with a
graph for any reading you like. Add them with **Add Metric**, or right-click a
reading (in the tree, or a ring, chip or row on a tile) and pick **Show in Live
Graphs**. Hover over a graph for the exact time and value; the span can be 1, 5
or 10 minutes. History is recorded from the moment the app starts, so a graph
you open later still shows what happened before.

## Other things

- **File → Save Monitoring Data** writes every reading with its min and max to
  a text file.
- Command line options:

  ```
  --interval SECONDS     refresh interval (default 1.0)
  --debug                verbose logging
  --no-dep-check         skip the startup check for optional utilities
  --install-fan-helper   install the fan control helper and exit
  --version
  ```

## Requirements

- Linux with Python 3.8 or newer (the AppImage brings its own)
- PyQt6 6.1+ and psutil 5.1+. Either `pip install -r requirements.txt` or your
  distribution's packages: `python3-pyqt6 python3-psutil` on Fedora, Debian and
  Ubuntu, `python-pyqt6 python-psutil` on Arch
- Optional: `lm_sensors` for better sensor coverage, `pciutils` for hardware
  names, `nvidia-smi` for NVIDIA cards, `polkit` for fan control

The AppImage doesn't carry libGL or graphics drivers on purpose, it uses your
system's. A bundled stack would run the GPU stress test on a software renderer.
It needs glibc 2.28 or newer, so anything from 2018 onwards.

## Running from source

```sh
git clone https://github.com/Tearmoon96/KSysSupervisor.git
cd KSysSupervisor
python3 KSysSupervisor.py
```

Nothing needs installing for monitoring; fan control needs the helper (see
above).

To run the tests (no special hardware needed, they use recorded sensor data
and fake sysfs trees):

```sh
python3 -m unittest discover -s tests
```

`tests/verify-fan-hardware.sh` runs the fan checks against your real fans and
puts them back afterwards.

To build it yourself: `./packaging/build-appimage.sh` makes the AppImage in
`dist/`, and `./packaging/make-release.sh` builds a full release in `release/`
(the Python archive, the AppImage and `SHA256SUMS`).

## License

[CC BY-NC-SA 4.0](LICENSE). You're free to use, modify and share it for
non-commercial purposes, as long as you credit it and keep derivatives under
the same license. Selling it, or something based on it, isn't allowed; if
you're interested in commercial use, get in touch.

The application icon is the exception and is **not** under CC BY-NC-SA: it's
copyright Tearmoon96, all rights reserved. It can only be shipped with unmodified
copies of KSysSupervisor, it can't be used in other projects, and forks or
modified versions have to replace it with their own. Details in
[LICENSE](LICENSE).
