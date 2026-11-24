# KSysSupervisor

A hardware monitor for Linux. Temperatures, fan speeds, clocks, voltages,
power draw and load for every device in the machine, each with its minimum and
maximum since the app started. Very similar to already existing Windows apps.

It also does manual fan control and has a stress test with live graphs. And it
has a disk utility: every drive and its health at a glance, and formatting
that's as simple as it is on Windows.

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
menu. The system-wide install also sets up the two small root helpers, for
fan control and for the disk utility; with a per-user install the app offers
to do that the first time you use either.

To remove it, run the uninstaller from the same folder (there's also a copy in
the install directory):

```sh
./uninstall.sh                      # removes it, asks for sudo if it needs to
./uninstall.sh --purge              # same, plus your settings
sudo ./uninstall.sh --fan-control   # only the root helpers
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

The **tree** is a simpler look: one branch per device, one row per
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

**Tools → Fan Control** lets you set fan speeds by hand. Together with the
disk utility further down, this is the only part of KSysSupervisor that
changes anything on your system; everything else only reads.

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
./KSysSupervisor-*-x86_64.AppImage --install-helpers   # AppImage
ksyssupervisor --install-helpers                      # installed with install.sh
python3 KSysSupervisor.py --install-helpers           # source folder
```

That installs the disk utility's helper as well, so it's one prompt for both.
(`--install-fan-helper`, the older name, still works.)

If you'd rather do it by hand from the source folder:

```sh
sudo install -Dm755 helpers/ksyssupervisor-fanhelper \
    /usr/local/lib/ksyssupervisor/ksyssupervisor-fanhelper
sed 's|@HELPER_PATH@|/usr/local/lib/ksyssupervisor/ksyssupervisor-fanhelper|' \
    packaging/org.ksyssupervisor.fancontrol.policy \
    | sudo tee /usr/share/polkit-1/actions/org.ksyssupervisor.fancontrol.policy
```

To remove the helpers: `sudo ./uninstall.sh --fan-control`, or
`sudo rm -r /usr/local/lib/ksyssupervisor /usr/share/polkit-1/actions/org.ksyssupervisor.*.policy`.

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


## Disk utility

**Tools → Disk Utility** shows every drive in the machine and what's on it,
and does the handful of things you'd otherwise need three different programs
and a terminal for. Linux disk tools are powerful and mostly look like it;
this one tries to be as plain as the format dialog on Windows.

On the left are the drives: hard disks, SSDs, NVMe drives, memory sticks and
card readers, each with its size, its temperature and a health verdict. Pick
one and you get its partitions as a bar and as a list, with the name, the
filesystem, where it's mounted and how full it is. A volume that isn't mounted
says so instead of guessing at its usage.

**Details** is underneath: model, serial number, firmware, connection, sector
sizes, and the health data the drive keeps about itself. For an SSD that
includes the life left as a percentage, how much has been written to it and
how long it has been powered on; for a hard disk, the sectors it has had to
replace. The verdict is Good, Caution or Bad, and it always says why. If a
drive doesn't report its wear (SATA SSDs each do it their own way) you get
"not reported" and not a number that might mean something else. All of this is
read through UDisks, so it needs no password.

What you can do to the selection:

- **Format.** Pick a filesystem (exFAT, FAT32, NTFS, ext4, Btrfs, XFS, F2FS)
  and a name, and that's it. On a partition you choose between that partition
  alone and the **whole drive, restored to one volume** - which is how a stick
  that was an installer, or is split into pieces, gets its full size back. The
  dialog warns you about choices you might regret while you make them: NTFS on
  a small memory stick, FAT32's 4 GB file limit, a Linux filesystem on a drive
  that has to work on Windows too, a name that's too long for the filesystem.
  A quick format just creates the new filesystem; a full one erases everything
  first, by overwriting a hard disk with zeros or by discarding every block of
  an SSD. For exFAT, FAT32 and NTFS you can also pick the **cluster size** -
  every file takes whole clusters, so it decides how much space small files
  waste. The usual size for the volume is preselected and marked, and the
  line under it says what any other size costs. A mounted exFAT, FAT32 or
  NTFS volume shows its current cluster size next to its usage.
- **Mount, Unmount, Eject.** Same as the file manager would do, in the same
  place.
- **Rename.** Changes a volume's name without touching what's on it.

Before anything is erased you get the exact list of what will be lost and what
is about to happen, step by step, and Enter is always Cancel. For a drive
that's built into the computer you also have to tick a box.

Under those buttons are four menus.

**Maintain** is upkeep. *Optimise* does what suits the drive: TRIM on an SSD,
a look at fragmentation on a hard disk. TRIM can be run now, and the system's
weekly TRIM switched on or off. Fragmentation is measured with the
filesystem's own tool before anything is offered, and the answer is usually
"not needed"; on an SSD defragmenting is strongly advised against and wants a
tick. *Check for Errors* looks without changing anything and only then offers
a repair, which is itself checked afterwards. *Wipe Free Space* overwrites
what isn't in use so deleted files can't be brought back, without touching the
files that are there. There is the drive's own self-test, and *What the
System Reports About This Drive*, which reads the kernel's complaints and
tells a failing drive from a bad cable.

**Copy** clones a drive or a partition onto another: every block, or only the
blocks in use where a tool can tell them (ext4, NTFS, XFS). The copy is read
back and compared unless you say not to, and a source with unreadable areas
can be copied around them, with the skipped ranges listed. Copying a drive
onto a larger one, you can tick *let the last partition use the free space*:
once the copy is checked, its last partition and the filesystem in it are
enlarged to the end of the new drive (ext4, Btrfs, XFS, NTFS, F2FS and swap;
the line under the tick says which partition and to what size, or why not).
A drive or
partition can be saved as an image file and written back, and an installer
`.iso` of a Linux system written to a memory stick. For a drive that is
failing there is *Rescue to an Image*, which copies the easy parts first and
goes back for the difficult ones, and an image can be opened as a read-only
drive so that everything else here works on the copy.

**Partitions** creates, deletes, resizes and moves partitions, and writes a
new table. A resize asks the filesystem's own tool how small it can go, checks
the filesystem before and after, and saves the previous table first. ext4,
NTFS and Btrfs grow and shrink; XFS and F2FS only grow; FAT32 and exFAT can't
be resized at all, and say so. A move can't be interrupted, but it keeps a
record as it goes, so one cut short by a power failure is finished afterwards
from where it stopped. *Search for Lost Partitions* finds filesystems that
are still on a drive without an entry in the table and puts the entries back.

**Recover Files** looks for what was lost, in two depths. *Quick Scan*
takes seconds: deleted files on NTFS, FAT32 and exFAT come back with their
names and folders. *Deep Scan* reads the whole drive: after a format, a drive
that was NTFS gives its files back in their folders, one that was FAT32 or
exFAT the files inside its folders (what was at the very top goes with the
format); then photos, documents, music and video are found by their content,
without names. It can be limited to some kinds, keep files that come back
damaged, or keep everything that only loosely looks like a file. The drive
is only read. A search is kept in one file that opens again without
searching twice - also on another computer - and where recovered files go
is chosen only when they are recovered. The results are shown in the folders
the files were in or by kind, with the same ticks on both; a Deep Scan
started from them adds to them. Recovered files are checked and marked
intact, partly damaged or unreadable, and damaged documents, PDFs and
pictures can be rebuilt from what is left of them. *Find
RAID Arrays* puts an array from a NAS or another computer together read-only,
also with a drive missing. *Put Drives Together as an Array* does it for a
RAID 0 or RAID 5 whose own record is gone - a dead hardware controller's,
for example: which drive comes first, the piece size and the parity layout
are worked out from the filesystem on the drives (NTFS, FAT32, exFAT or
ext4), and it says so when it can't be sure.

Two things no program can do, this one included: bring back files deleted
from an SSD that has been trimmed since, and read a drive whose own firmware
has failed. Both are said up front instead of after a long search.

A few things it will not do, on purpose:

- **Nothing destructive on the system drive.** The volumes your running system
  lives on (`/`, `/boot`, `/home`, swap and so on) can't be formatted,
  unmounted, renamed, resized or checked, and the drive they are on can't be
  cloned: a drive in use can't be copied reliably. The entries are greyed out
  and tell you why, and the helper that does the actual work refuses on its
  own even if the window somehow asked. TRIM, defragmenting and wiping free
  space are safe there and stay available. To do the rest to the system
  drive - moving to a new SSD, for one - start the computer from a
  recovery stick (below).
- **Windows volumes are handled with care.** One that was hibernated, or
  closed by "fast startup", is not changed, and the message says what to do in
  Windows. A BitLocker volume is shown as encrypted. After a resize, Windows
  checks the drive at its next start, which is expected.
- **It doesn't trust a device name.** `sdd` is whatever was plugged in fourth.
  If you swap one memory stick for another between looking at it and
  confirming, the format is refused and not carried out on the wrong one.
- **It doesn't take a tool's word for it.** After a format the device is read
  back and compared with what you asked for.
- **It tells a slow drive from a stuck one.** While an operation runs,
  **Details** shows what the tool is doing ("Writing inode tables: 412/924"),
  how much has been written and how fast. If the drive stops answering for a
  minute you get a warning, along with what the system reported about the
  drive and whether it is safe to unplug it.

Like fan control, anything that writes goes through a small helper that runs
as root and is authorised through polkit: one password prompt per session. It
installs the same way, and the same prompt installs both helpers. The actual
work is done by the standard tools (`mkfs.*`, `sfdisk`, `wipefs`, `e2fsck`,
`ntfsresize` and the like), so whatever needs a tool that isn't installed is
greyed out with the name of the package to install. Recovery uses The Sleuth
Kit, TestDisk/PhotoRec and `ddrescue` (`sleuthkit`, `testdisk`, `ddrescue`),
and `mdadm` for RAID.

On Plasma, **KSysSupervisor Disk Utility** also appears when you right-click a
drive in the file manager's Places panel, and in the Device Notifier. The
install script sets that up; with the AppImage, turn it on from the menu
button next to **Refresh**.

### Recovery stick

**Tools → Make a Recovery Stick** (or **Copy → Make a Recovery Stick on
This** on a USB stick) turns a stick of 1 GB or more into a small system
that starts a computer straight into the Disk Utility, full screen. It is the
way to clone, resize or repair the drive your system runs from, since
nothing on that drive is in use while the stick runs.

The stick is put together from your own computer: its kernel, its Python and
Qt, its disk tools. Nothing is downloaded, it takes about 150 MB, and it
starts in a few seconds. It uses your distribution's signed boot loader, so
it starts with Secure Boot on (Fedora, Ubuntu, Debian, openSUSE); without
one, it is built anyway and Secure Boot has to be off. It needs a computer
with UEFI - anything from about 2012 on - and 2 GB of memory. The stick stays
an ordinary FAT32 stick: the rest of it holds files as before.

On the stick nothing happens the moment you click. Each change you set up -
a format, a clone, a resize, a new partition - goes into a list at the
bottom of the screen, and the drives are shown as they will be: a deleted
partition is gone from the bar, a new one is there, striped until it is real.
You can set up the next change on top of that, take one back, or discard
them all. **Apply Changes** shows the whole list once more and then carries
it out in order, stopping at the first thing that goes wrong. The stick
itself can't be changed from the stick, and what the helper needs to finish
a moved partition after a power cut is kept on it.

Drive health isn't shown on the stick (it comes from UDisks, which the
stick doesn't have), and its keyboard is the one your computer uses, with a
choice of others at the top of the screen.

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
  --install-helpers      install the fan control and disk helpers and exit
  --disk-utility [DEV]   open only the Disk Utility, optionally on a device
  --version
  ```

## Requirements

- Linux with Python 3.8 or newer (the AppImage brings its own)
- PyQt6 6.1+ and psutil 5.1+. Either `pip install -r requirements.txt` or your
  distribution's packages: `python3-pyqt6 python3-psutil` on Fedora, Debian and
  Ubuntu, `python-pyqt6 python-psutil` on Arch
- Optional: `lm_sensors` for better sensor coverage, `pciutils` for hardware
  names, `nvidia-smi` for NVIDIA cards, `polkit` for fan control and the disk
  utility
- For the disk utility: `udisks2` (drive health, mounting; every desktop has
  it) and, for each filesystem you want to create, its tools: `e2fsprogs`,
  `dosfstools`, `exfatprogs`, `ntfs-3g` or `ntfsprogs`, `btrfs-progs`,
  `xfsprogs`, `f2fs-tools`. The ones that are missing are simply greyed out

The AppImage doesn't carry libGL or graphics drivers on purpose, it uses your
system's. A bundled stack would run the GPU stress test on a software renderer.
It needs glibc 2.28 or newer, so anything from 2018 onwards.

## Running from source

```sh
git clone https://github.com/Tearmoon96/KSysSupervisor.git
cd KSysSupervisor
python3 KSysSupervisor.py
```

Nothing needs installing for monitoring; fan control and the disk utility
need their helpers (see above).

To run the tests (no special hardware needed, they use recorded sensor data
and fake sysfs trees):

```sh
python3 -m unittest discover -s tests
```

`tests/verify-fan-hardware.sh` runs the fan checks against your real fans and
puts them back afterwards. `sudo tests/verify-disk-tools.py` runs the disk
helper for real, but only ever against loop devices it creates itself from
files in a temporary folder: it formats them with every filesystem and checks
each result with `blkid`, `fsck` and a mount. No real drive is touched.

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
