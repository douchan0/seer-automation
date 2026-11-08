# Tower template provenance

The detector uses 2400×1600 game-window pixels at the calibrated client geometry.
The PNG additions are tight crops of game sprite art or floor geometry. They
contain no chat, account UI, title bar, desktop content, or complete captures.

| Template | Historical game scene | Crop in game_px |
|---|---|---|
| tower_guardian_01 | Floor 01, central pink and green guardian | (1190, 710) to (1245, 843) |
| tower_guardian_02 | Floor 02, central blue guardian | (1198, 724) to (1244, 837) |
| tower_guardian_03 | Floor 03, central orange guardian | (1189, 713) to (1234, 850) |
| tower_guardian_14 | Floor 14, central guardian | (1187, 663) to (1230, 840) |
| tower_clear_left | Floor 01, platform detail | (950, 900) to (1080, 972) |
| tower_clear_right | Floor 01, platform detail | (1305, 900) to (1435, 972) |

Coordinates have a top-left origin and the end is exclusive. The locator requires
a matching painted floor, then searches the current frame. Template scores
measure image similarity. Unsupported floors and obstructed scenes stop the runner.
