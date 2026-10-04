# Omniscient

Omniscient is an early browser-based foundation for a visual security platform. The current prototype accepts a local video file or a live camera, performs frame-difference motion analysis in the browser, assigns short-lived IDs to moving regions, and raises contextual security insights for entry, restricted-area, and extended-presence activity.

## Run locally

Serve this directory from localhost, then open it in a modern browser:

```sh
python3 -m http.server 8000
```

Visit `http://localhost:8000`. Choose **Upload video** to analyze a recorded clip, or **Use camera** and grant camera access. Camera access generally requires localhost or HTTPS. Video is processed locally and is not uploaded.

## Prototype scope

- Frame differencing, connected motion regions, and nearest-centroid track association run in the browser.
- The two demo zones are fixed to the left and right portions of the video frame.
- Insights include motion detection, zone entry, restricted-area activity, and presence longer than eight seconds.
- This prototype does not identify object classes, recognize people, preserve identity across occlusion, store footage, or contact emergency services. Motion boxes can include shadows, lighting changes, and camera movement.

The current app uses plain HTML, CSS, and JavaScript, with no build step or external runtime dependency.
