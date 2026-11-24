# Changelog

All notable changes to KSysSupervisor. Versions follow
[semantic versioning](https://semver.org); each release is tagged `v<version>`
on GitHub and ships as an AppImage and as a Python archive with
`install.sh` / `uninstall.sh`.

## Unreleased

- Disk Utility (Tools menu): every drive and volume with its filesystem,
  mount point and usage, and each drive's health from its SMART data - SSD
  life left, data written, power-on hours, reallocated sectors - with a Good,
  Caution or Bad verdict and the reasons for it. No password needed to look.
- Formatting, of a whole drive or one partition, as exFAT, FAT32, NTFS, ext4,
  Btrfs, XFS or F2FS, quick or full, with warnings for choices that tend to be
  regretted. Mount, unmount, eject and rename.
- Format can restore a drive that is split up, or was written as an installer
  image, to one volume of its full size.
- Format and New partition offer every cluster size an exFAT, FAT32 or NTFS
  volume can have, with the usual one recommended and the cost of the others
  explained; the size made is read back from the new filesystem. Mounted
  volumes of these filesystems show their current cluster size.
- Maintain: TRIM now and the weekly TRIM on or off; fragmentation measured
  before defragmenting is offered, with a strong warning on SSDs; a check for
  errors that changes nothing, then a repair that is checked again; wiping of
  free space; the drive's self-test; the kernel's messages about a drive put
  into plain words. Drive health and SSD life left also show in the main
  window, with a notification when a drive's verdict gets worse.
- Copy: clone a drive or partition, every block or used blocks only, verified
  by reading back, optionally skipping unreadable areas; save and write image
  files, including installer images; rescue a failing drive to an image; open
  an image as a read-only drive.
- Partitions: new table, create, delete, resize and move, with the previous
  table saved before every change. A move that is interrupted is finished
  from where it stopped. Search for lost partitions and put them back.
- Recover Files: deleted files with their names on NTFS, FAT32 and exFAT;
  after a format, an NTFS drive's files in their folders, and files of any
  kind by content; results by folder or by kind; recovered
  files checked, and damaged documents, PDFs and pictures repaired; RAID
  arrays with intact metadata opened read-only, also degraded.
- Recover Files searches at two depths: Quick Scan (deleted files with their
  names, in seconds) and Deep Scan (the whole drive; can be limited to
  photos, video, music, documents or archives, keep damaged files, or keep
  unrecognised data). A search is kept in one file that reopens without
  searching again, also on another computer; the folder to recover into is
  chosen when recovering. Warnings come before notes and open on a click.
- Fixed: the stress test's temperature warning lost its last line.
- After a format, the files inside the folders of a drive that was FAT32 or
  exFAT come back by name too. NTFS written by Linux's newer driver (ntfs3,
  now the default on Fedora) is read as well.
- RAID 0 and RAID 5 whose own record is lost: how the drives fit together is
  worked out from the filesystem on them, and the array is opened read-only,
  also with a drive missing.
- Volumes the running system uses, and Windows volumes left hibernated or
  encrypted, are greyed out or refused with what to do about it.
- Recovery stick (Tools menu): turns a USB stick into a small system, put
  together from this computer's own kernel, Python, Qt and disk tools, that
  starts any UEFI computer straight into the Disk Utility - also with Secure
  Boot on, through the distribution's signed boot loader. On the stick,
  changes are prepared first and shown on the drives as they will be, and
  made only by Apply Changes; this is the way to clone or resize the drive
  a system runs from.
- Clone: onto a larger drive, the last partition and its filesystem can be
  enlarged into the free space once the copy is checked.
- Fixed: resizing or moving a partition on an MBR drive larger than 2 TiB
  failed late, inside sfdisk, instead of being kept within the 2 TiB an MBR
  can describe.
- A live USB system's own stick is treated as part of the running system,
  so it can't be formatted from the AppImage running on it.
- Changes go through a second small root helper authorised with polkit. It
  refuses the volumes the running system uses, refuses a device that was
  swapped for another after it was selected, and reads every result back
  before reporting success. One prompt installs it together with the fan
  helper; `--install-helpers` does the same from a terminal.
- "KSysSupervisor Disk Utility" in the right-click menu Plasma shows for a
  drive, a "Disk Utility" entry on drives in the tree and the tiles, and
  `--disk-utility [DEVICE]` to open it on its own.
- While a disk operation runs, Details shows each step as the tool reports
  it ("Writing inode tables: 412/924"), with the amount written, the speed
  and a log. If a drive stops answering for a minute, a warning appears with
  what the system reported about the drive and what is safe to do, plus a
  desktop notification when the window is in the background.
- Fixed: in Format and the other disk dialogs, a warning that appeared after
  a choice was made was squeezed into one line, along with the text above it,
  until it was expanded.
- Fixed: a device name given exactly the room it asked for could still be
  shortened with an ellipsis.
- Fixed: after the distribution moved to a new Python version (for example
  Fedora 45, Python 3.15), `install.sh` and updates kept the old virtual
  environment, whose installed packages the new Python no longer finds. It
  is now made again.

## 1.0.0

First public release.

- Hardware monitor for Linux: temperatures, fans, voltages, clocks, power,
  utilisation and memory for CPU, GPU (AMD, NVIDIA, Intel), mainboard, RAM,
  drives and battery, in a classic tree or a tile layout.
- Live graphs of any reading, and File > Save Monitoring Data.
- Manual fan control through a small root helper authorised with polkit; every
  fan goes back to automatic control when the app exits, even if it crashes.
- CPU, memory and GPU stress test with an automatic temperature limit.
- Hardware setup tips for sensors that need a kernel module.
- Update check at startup (can be turned off) and on demand in
  Help > Check for Updates; updates install themselves, verified against the
  release's checksums, and keep your settings.
