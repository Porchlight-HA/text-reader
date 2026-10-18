# Text Reader for Home Assistant

Read text from camera snapshots, images and files. Text Reader is a Home Assistant integration with one response action, `text_reader.read_image`, which returns the recognised text and, with a preset, structured fields (envelope addressee, meter value, package tracking numbers). It reads on your own hardware with the **Local OCR App** (a small OCR server built on RapidOCR), with an AI assistant you already set up in Home Assistant, or with both.

## Pick how it reads

Setup asks one question, "How should Text Reader read images?", and you can change the answer later in the integration's options.

| Choice | What it does | Good for |
|---|---|---|
| **On this device (private)** | Reads with the Local OCR App. Nothing leaves your network. This is the default when the App is present. | Mail and labels on a Pi 4 or better. Text, boxes and confidence. |
| **With an AI assistant you already set up** | Sends every image to the AI Task entity you pick (Ollama, OpenAI, Google, Anthropic, OpenRouter, Home Assistant Cloud). No App needed. No boxes, `confidence` is null. | Boxes too small for the App, messy handwriting, or hardware that can't run the App. |
| **Both (recommended if you have both)** | Reads on this device first and asks the assistant only when the local read is weak. | Local speed and privacy, with AI for the hard images. |

In "both" mode (`auto`), an image goes to the assistant only if the App is unavailable, the local read found no text, the preset's key field is missing (envelope addressee, label tracking number, meter value), or the mean block confidence is below `auto_threshold` (default 0.8). With `all_frames` this is decided per frame. An image the App rejects (not a valid image, too large) is never retried with the assistant.

### Requirements

| Setup | Needs | Measured |
|---|---|---|
| Local | 64-bit Home Assistant OS or Container (aarch64 or amd64). **2 GB+ RAM** (the App uses 224 MB loaded and about 57 MB idle; Home Assistant plus the App used 1.25 GB on a Pi 4). About 250 MB of disk. A Pi 4 or better. | Pi 4: about 4.2 to 4.6 s per image, a 12-frame GIF in 56 s, a 3 min 19 s on-device build. amd64 laptop: about 0.5 s per image. |
| AI only | Any Home Assistant 2026.10+ install with an AI Task entity that accepts attachments. No App. | Depends on the assistant. |
| Both | The local requirements plus an AI Task entity. | Local speed, plus AI only for hard images. |

A 1 GB board or a 32-bit install can't run the App and should use the AI-only choice. On a Supervisor install, setup steers you there automatically when the system is not 64-bit or has too little memory, and the `local_unsupported_here` repair offers the switch if you already chose local. On Container or Core installs this check is skipped, because the OCR server can run on another machine.

### Privacy

**Local is the default, and nothing leaves your network with it.** Choosing an AI assistant is an explicit step: setup and the options ask you to confirm that images are sent to that assistant, and Text Reader never uses an assistant without that confirmation.

- In **AI-only** mode every image is sent to the assistant you chose. A cloud assistant (OpenAI, Google, Anthropic and so on) sends it to that provider.
- In **both** mode only images the device couldn't read well are sent, **or all images while the Local OCR app is unreachable**.
- Calls with `backend: local` never use the assistant. A call with `backend: ai_task` sends the image to the assistant whatever the default is.
- **Ollama is the recommended choice if you want AI that stays local**: it runs on your own machine, so the images never leave your network.
- Images and recognised text are never stored or logged by Text Reader. Images sent to an assistant are written to a private temporary folder (`.text_reader_tmp` in your config folder, owner-only) and deleted as soon as the call ends.
- Assistant integrations themselves may log their replies when they hit errors (for example Ollama and OpenAI log unparseable replies), which can include recognised text.
- Diagnostics contain no text and no image data.
- **Automation traces keep the response.** See Privacy details below.

## Upgrading from Local OCR 0.2

Text Reader 0.3 is the renamed successor of the "Local OCR" integration (domain `local_ocr`). The domain changed, so there is **no automatic migration**:

1. Remove the old **Local OCR** integration (Settings, Devices and services, Local OCR, Delete) and uninstall its HACS download.
2. Install **Text Reader** from HACS and add it (see Install).
3. In automations and scripts, change `local_ocr.read_image` to `text_reader.read_image`. The existing fields are unchanged; `routed` and `confidence` are added. Entity ids may change with the domain, so check dashboards that use the status and duration sensors.

The Local OCR **App** keeps its name and slug. Update it to 0.3.0 along with the integration; it then announces itself as `text_reader`.

## Install

1. **Add the App repository** (skip if you only use an AI assistant).
   [![Add the Local OCR App repository to your Home Assistant.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https://github.com/Porchlight-HA/local-ocr)
2. **Install the App** and start it.
   [![Show the Local OCR App in your Home Assistant.](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=490c75da_local_ocr)
   <!-- The addon slug 490c75da_local_ocr was confirmed on a live Supervisor (Pi 4, HAOS 18.3) on 2026-10-18. -->
   If the button does not open the App, open Settings > Apps > Install app, then Local OCR. Or skip steps 1 and 2: choosing "On this device" or "Both" in setup installs the Local OCR app for you (it downloads a prebuilt image; on a Pi 4 it took under a minute from choosing "On this device" to a passing sample test). If that can't happen, setup shows the manual steps (the My-HA button) and waits for the app.
3. **Install the integration via HACS.**
   [![Open the Text Reader repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Porchlight-HA&repository=text-reader&category=integration)
   Restart Home Assistant after the download.
4. **Add Text Reader and answer the question.** If the App is running, Home Assistant may first show "Local OCR found: Set up?". Select Submit. Setup tests itself on a bundled synthetic sample (with the assistant too, if you picked one) before saving anything.
   If no prompt appears (for example you use a Container install), add the integration by hand:
   [![Start the Text Reader config flow.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=text_reader)

### Container users

If you run Home Assistant Container (no Supervisor) you run the OCR server as a plain Docker container. The App's `DOCS.md` in [Porchlight-HA/local-ocr](https://github.com/Porchlight-HA/local-ocr) has the exact `docker run` command, including how to set `LOCAL_OCR_TOKEN` so the API requires a bearer token. Then add the integration, choose "On this device" or "Both", and enter `http://<docker-host>:8738` plus the token. Container users who only want an AI assistant need no server at all.

## The action: `text_reader.read_image`

A response action: call it with `response_variable` and use the result in the rest of your automation or script.

Give **exactly one** image source:

| Field | Meaning |
|---|---|
| `image_entity` | An `image.*` entity |
| `camera_entity` | A `camera.*` entity (a snapshot is taken) |
| `file_path` | A file path in an allowed folder. Home Assistant allows `/config/www` and its media folders (`/media` on Home Assistant OS) by default; add others to `allowlist_external_dirs` |
| `media_content_id` | A media-source id |

Optional fields:

| Field | Meaning |
|---|---|
| `config_entry_id` | Which Text Reader entry to use. Only needed with more than one |
| `preset` | `text`, `envelope`, `meter` or `label`. Defaults to the integration option |
| `region` | `{x, y, w, h}` crop applied before OCR. See below |
| `backend` | `local`, `ai_task` or `auto`. Defaults to the integration option |
| `min_confidence` | 0 to 1. Drops blocks below this confidence. Defaults to the integration option (0.5). Applies to the local reader only (the assistant ignores it) |
| `all_frames` | For animated images, read every frame instead of the first |

```yaml
action: text_reader.read_image
data: {camera_entity: camera.porch, preset: label}
response_variable: ocr
```

Reading a file, for example a scanned envelope:

```yaml
action: text_reader.read_image
data:
  file_path: /media/scans/envelope.jpg
  preset: envelope
response_variable: ocr
```

`/media` and `/config/www` are allowed by default (in Home Assistant 2026.10, `allowlist_external_dirs` always includes `/config/www` and the `media_dirs`). Prefer `/media` for scans of mail: anything in `/config/www` is served without a login at `/local/`. Any other folder must be added in `configuration.yaml`:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /config/scans
```

### Region semantics

`region` crops the image before recognition (for both readers). If **all four** values are 1 or less they are **fractions of the image**, so `{x: 0, y: 0, w: 1, h: 1}` is the whole image and `{x: 0.5, y: 0, w: 0.5, h: 1}` is the right half. Otherwise all four are **pixels**. Mixing the two (for example `x: 0.1` with `w: 300`) is rejected.

### Presets

| Preset | Returns |
|---|---|
| `text` (default) | `text` (the recognised lines) and `blocks` (each with `text`, `confidence`, `box`, `line`) |
| `envelope` | Everything in `text`, plus `fields`: `addressee`, `return_address`, `indicia`, `markers` (USPS bulk-mail tokens such as PRSRT STD or ECRWSS) and `generic_addressee`. These are hints only; the decision what to do with a piece of mail is yours |
| `meter` | Digits and the decimal point only, as `fields.value` (a number, or null if unparseable), `fields.digits` (the recognised digits as a string) and `fields.confidence` (one number for the whole reading). Use `region` to crop to the dial |
| `label` | `fields.tracking`: a list of `{carrier, number}` (UPS, USPS, FedEx, DHL), plus the ship-to block |

Tracking numbers in `label`, and their limits:

- **USPS and FedEx** numbers are found when printed solid or in groups of four digits ("9400 1118 9922 …", "1234 5678 9012"). Other digit runs on the same line are never joined, so a ZIP code next to a phone number can't turn into a false FedEx number.
- **FedEx and DHL** numbers are reported only when the word FEDEX or DHL also appears on the label, because their formats are bare digit runs.
- **UPS** numbers (`1Z` plus 16 characters) are found only when the OCR reads them without spaces. A label printed as "1Z 999 AA1 01 2345 6784" is not matched; take the number from `text` if you need it.

## Text extraction contract v1

Text Reader implements this contract. Any other integration can implement it too, and consumers can then accept any "text extractor" without depending on Text Reader.

- A **response-returning action**. It accepts `image_entity`/`file_path`/`media_content_id` and an optional `preset`, and returns at least `text: str`. Optionally it also returns `blocks: [{text, confidence, box, line}]` and `fields: {…}`.
- Text Reader also returns `routed` and `confidence` (below).
- `text` has one line per visual line of the image, top to bottom. Blocks on the same line (same `line` number) are joined with a single space; lines are separated by `\n`.
- Consumers must work with `text` alone and use `fields` when present.

With `all_frames: true` the response is different: `{"frames": [<one result per frame, as above>], "text": "<each frame's text, joined with \n\n>"}`, plus Text Reader's `routed` and `confidence` (below). There are no other top-level fields, whichever reader answered; `width`, `blocks`, `fields` and so on are inside each frame. Reading every frame takes about one read per frame, so the action allows it at least 180 s.


### `routed`, `confidence` and AI results

Text Reader adds two fields to the v1 result (consumers that ignore them are unaffected):

| Field | Meaning |
|---|---|
| `routed` | Who read the image: `"local"` (the App), `"ai_task"` (the assistant) or `"local→ai_task"` (the App was tried and the assistant took over, in "both" mode). |
| `confidence` | The mean confidence of the local blocks, or `null` when an assistant read it (also `null` if any frame of an `all_frames` read went to the assistant). |

An assistant's result has `engine: "ai_task:<entity_id>"`, `model: null`, **no boxes** (`blocks` is empty) and `confidence: null`. Presets still work: the assistant is asked for the same `fields` (addressee, tracking numbers, meter value), and they have the same shape as the App's: `text` has `fields: null`; an envelope's `indicia` is always an empty list (the assistant isn't asked for it); a meter's `fields.confidence` is `null`; an address block that wasn't found is `null`. `width` and `height` are the image's size after EXIF rotation, before any `region` crop, as with the App. Text Reader sends the assistant one frame (the first, unless `all_frames`), rotated upright and cropped to `region`, as PNG (JPEG for photos over 2 MP), or the original file when it is already a single-frame JPEG, PNG or WEBP that needs neither. Images over 40 megapixels are rejected, as by the App. Each assistant call may take at least 120 s (or the timeout option, if longer); after that it counts as unavailable, and in "both" mode the local result is kept. The instructions sent to the assistant are fixed and versioned (diagnostics shows `ai_prompt_version`); they can't be edited.

## Repairs

Repairs appear under Settings, System, Repairs and clear themselves when the problem goes away. The first four apply only when the App is in use.

| Repair | Meaning | Fix |
|---|---|---|
| `app_not_running` | The App did not answer three health checks in a row. | App users: select **Submit** in the repair to start it, or start it from the App page. Container users: `docker start local-ocr`. |
| `app_outdated` | The App's API version is older than this integration needs. | Update the Local OCR App, then reload the integration. |
| `model_failed` | The App is running but its OCR model failed to load. | Restart the App. If it keeps happening, reinstall the App (models are bundled, nothing is downloaded). |
| `low_memory` | The App ran out of memory in the last hour: a model load or a read failed for lack of memory, or the system killed the App for using too much. Shown instead of `model_failed` when memory is the cause. | Stop other apps, give the machine more RAM, or lower `max_side` in the App options. |
| `ai_entity_missing` | The AI Task entity you chose was removed or renamed. | Select **Submit** and pick another assistant (you confirm the privacy notice again). |
| `ai_entity_unavailable` | The assistant has not answered for three checks in a row. In "both" mode Text Reader keeps reading on this device only. With AI-only it can't read until the assistant is back. | None needed; it clears when the assistant is back. |
| `local_unsupported_here` | You chose a local choice, but this Supervisor system is not 64-bit or has less than 2 GB of memory. | Select **Submit** to switch Text Reader to the AI assistant you already chose (if there is none, set one up and pick it in the options). |

## Privacy details

Text Reader never stores or logs images or recognised text. The App writes only its access token to disk. Text Reader's logs contain sizes and timings, never content. Assistant integrations themselves may log their replies when they hit errors (for example Ollama and OpenAI log unparseable replies), which can include recognised text. Diagnostics (Settings, Devices and services, Text Reader, Download diagnostics) contain the backend, the AI entity id, the auto threshold, the prompt version, the App's version and health, and the last 20 requests as timing, preset, the requested `backend`, the actual `routed`, image size, block count and error class. They contain **no text**, and the token is redacted.

**Automation traces keep the response.** Home Assistant stores the last traces of each automation and script in `.storage/trace.saved_traces`, including `response_variable` values, so recognised text ends up there. For automations that read sensitive images (mail, for example), turn stored traces off for that automation:

```yaml
alias: Read the mail scan
trace:
  stored_traces: 0
```

Or keep only what you need: copy the fields you use into a variable and don't keep the whole response around.

When you report a problem, please attach a **synthetic** repro image, never a real photo of your mail.

## What it can't read well

- **Thin felt-tip handwriting** at Informed Delivery resolution (about 780 px wide). Neat handwriting at higher resolution often works; do not rely on it.
- **Seven-segment LCD displays.** General OCR is weak on those glyphs. Use [ssocr](https://www.unix-ag.uni-kl.de/~auerswal/ssocr/) or [AI-on-the-edge-device](https://github.com/jomjol/AI-on-the-edge-device) instead.

Printed text on envelopes, labels and signs is the sweet spot. For the cases above an AI assistant (the AI or both choice) may do better.

## Known limitations

With some assistant integrations, Home Assistant may log a "blocking call" warning while it reads the image; this comes from that integration.

## Licence

MIT. See [LICENSE](LICENSE).
