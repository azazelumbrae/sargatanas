## Description

<!-- What does this change, and why? -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Security change (affects an isolation profile - explain below)
- [ ] Documentation
- [ ] Refactor

## Security impact

<!--
If this touches sargatanas/profiles.py or sargatanas/qemu.py, describe what a
guest can now reach that it could not before. Profiles must refuse to launch
rather than silently degrade.
-->

## Testing

- [ ] `python -m unittest discover -s tests -t .` passes
- [ ] Tested on: <!-- Linux / macOS / Windows -->
- [ ] If networking changed: booted a live ISO and verified connectivity
