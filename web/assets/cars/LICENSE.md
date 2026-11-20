# Car models

No car model ships with this project. The 3D scene builds a procedural 2026-style car at run time
(`web/scene/procedural_car.js`), which is original work with no third-party geometry.

To use your own model, put a glTF binary at `web/assets/cars/f1_2026.glb` and list it as `car_glb` in
`web/assets/local_index.json` (written by `scripts/fetch_assets.py`). Only add a model you have the right
to use, and record its author, source URL and licence in this file. Do not add models that carry team
logos, sponsor marks or other trademarks.
