# Heartbeat Estate — 3D Environment

An interactive 3D illustration of the Heartbeat Estate: a manor with grounds,
a gravel drive, garden lamps, and a beating heart monument rising from the
central fountain. The whole scene pulses in sync with a configurable heart
rate, and a live EKG monitor strip runs along the bottom of the screen.

## Running

No build step, no dependencies, no server required — everything (including the
3D engine) lives in a single file:

```
open heartbeat-estate/index.html
```

or just double-click `index.html` in any modern browser.

## Controls

| Action        | Input                          |
|---------------|--------------------------------|
| Orbit camera  | Click / touch and drag         |
| Zoom          | Mouse wheel                    |
| Heart rate    | BPM slider (42–160)            |
| Time of day   | `Day` / `Dusk` buttons         |
| Auto-rotate   | `Spin` button                  |

URL parameters let you link to a specific view, e.g.
`index.html?day=1&bpm=96&yaw=2.4&pitch=0.5&dist=80`.

## How it works

The renderer is a small hand-written 3D engine on a 2D canvas:

- Perspective projection with an orbiting camera and near-plane
  (Sutherland–Hodgman) polygon clipping.
- Painter's-algorithm depth sorting of flat-shaded polygons, with a separate
  early pass for ground decals and a sort bias for faces mounted on walls
  (windows, door).
- Directional sun/moon lighting with ambient, night tinting, and distance fog.
- Additive radial-gradient sprites for the heart's glow and lamp light.
- A shared cardiac phase (twin-peaked "lub-dub" pulse) drives the heart
  monument's scale, the glow intensity, window/lamp flicker, and the PQRST
  trace on the monitor strip, so the entire estate beats as one.
