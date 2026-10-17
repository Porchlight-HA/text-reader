# Local OCR for Home Assistant

Read text from camera snapshots, images and files, entirely on your own hardware. Local OCR is a Home Assistant integration that talks to the **Local OCR App** (a small OCR server built on RapidOCR). It adds one response action, `local_ocr.read_image`, which returns the recognised text, positioned text blocks and, with a preset, structured fields (envelope addressee, meter value, package tracking numbers).

Nothing leaves your network, and no cloud account or API key is involved.

## Install

1. **Add the App repository.**
   [![Add the Local OCR App repository to your Home Assistant.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https://github.com/Porchlight-HA/local-ocr)
2. **Install the App** and start it.
   [![Show the Local OCR App in your Home Assistant.](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=490c75da_local_ocr)
   <!-- The addon slug 490c75da_local_ocr is computed (sha1 of the lowercased repo URL, first 8 hex chars), not verified on a live Supervisor. -->
   If the button does not open the App, open Settings > Apps > Install app, then Local OCR.
3. **Install the integration via HACS.**
   [![Open the Local OCR repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Porchlight-HA&repository=ha-local-ocr&category=integration)
   Restart Home Assistant after the download.
4. **Accept the discovery prompt.** Home Assistant shows "Local OCR discovered: Set up?". Select Submit. The setup tests itself on a bundled synthetic sample before saving anything.
   If no prompt appears (for example you use a Container install), add the integration by hand:
   [![Start the Local OCR config flow.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=local_ocr)

### Container users

If you run Home Assistant Container (no Supervisor) you run the OCR server as a plain Docker container. The App's `DOCS.md` in [Porchlight-HA/local-ocr](https://github.com/Porchlight-HA/local-ocr) has the exact `docker run` command, including how to set `LOCAL_OCR_TOKEN` so the API requires a bearer token. Then add the integration and enter `http://<docker-host>:8738` plus the token.

## The action: `local_ocr.read_image`

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
| `config_entry_id` | Which Local OCR entry to use. Only needed with more than one |
| `preset` | `text`, `envelope`, `meter` or `label`. Defaults to the integration option |
| `region` | `{x, y, w, h}` crop applied before OCR. See below |
| `min_confidence` | 0 to 1. Drops blocks below this confidence. Defaults to the integration option (0.5) |
| `all_frames` | For animated images, read every frame instead of the first |

```yaml
action: local_ocr.read_image
data: {camera_entity: camera.porch, preset: label}
response_variable: ocr
```

Reading a file, for example a scanned envelope:

```yaml
action: local_ocr.read_image
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

`region` crops the image before recognition. If **all four** values are 1 or less they are **fractions of the image**, so `{x: 0, y: 0, w: 1, h: 1}` is the whole image and `{x: 0.5, y: 0, w: 0.5, h: 1}` is the right half. Otherwise all four are **pixels**. Mixing the two (for example `x: 0.1` with `w: 300`) is rejected.

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

local_ocr implements this contract. Any other integration can implement it too, and consumers can then accept any "text extractor" without depending on local_ocr.

- A **response-returning action**. It accepts `image_entity`/`file_path`/`media_content_id` and an optional `preset`, and returns at least `text: str`. Optionally it also returns `blocks: [{text, confidence, box, line}]` and `fields: {…}`.
- `text` has one line per visual line of the image, top to bottom. Blocks on the same line (same `line` number) are joined with a single space; lines are separated by `\n`.
- Consumers must work with `text` alone and use `fields` when present.

With `all_frames: true` the response is different: `{"frames": [<one result per frame, as above>], "text": "<each frame's text, joined with \n\n>"}`. There are no other top-level fields; `width`, `blocks`, `fields` and so on are inside each frame. Reading every frame takes about one read per frame, so the action allows it at least 180 s.

## Repairs

Repairs appear under Settings, System, Repairs and clear themselves when the problem goes away.

| Repair | Meaning | Fix |
|---|---|---|
| `app_not_running` | The App did not answer three health checks in a row. | App users: select **Submit** in the repair to start it, or start it from the App page. Container users: `docker start local-ocr`. |
| `app_outdated` | The App's API version is older than this integration needs. | Update the Local OCR App, then reload the integration. |
| `model_failed` | The App is running but its OCR model failed to load. | Restart the App. If it keeps happening, reinstall the App (models are bundled, nothing is downloaded). |
| `low_memory` | The App ran out of memory in the last hour: a model load or a read failed for lack of memory, or the system killed the App for using too much. Shown instead of `model_failed` when memory is the cause. | Stop other apps, give the machine more RAM, or lower `max_side` in the App options. |

## Privacy

Images and recognised text are **never stored or logged**. The App writes only its access token to disk. Logs contain sizes and timings, never content. Diagnostics (Settings, Devices and services, Local OCR, Download diagnostics) contain the App's version and health plus the last 20 requests as timing, preset, image size, block count and error class. They contain **no text** and the token is redacted.

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

Printed text on envelopes, labels and signs is the sweet spot.

## Licence

MIT. See [LICENSE](LICENSE).
