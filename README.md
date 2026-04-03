# bambu-3mf

Package plain G-code into Bambu Lab `.gcode.3mf` files.

## Usage

```python
from bambu_3mf import pack_gcode_3mf, SliceInfo, FilamentInfo

gcode = Path("plate_1.gcode").read_bytes()

info = SliceInfo(
    nozzle_diameter=0.4,
    filaments=[FilamentInfo(filament_type="PLA")],
)

pack_gcode_3mf(gcode, Path("output.gcode.3mf"), slice_info=info)
```

## CLI

```
bambu-3mf plate_1.gcode -o output.gcode.3mf
```
