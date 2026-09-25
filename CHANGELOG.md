# Changelog

All notable changes to KSysSupervisor. Versions follow
[semantic versioning](https://semver.org); each release is tagged `v<version>`
on GitHub and ships as an AppImage and as a Python archive with
`install.sh` / `uninstall.sh`.

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
