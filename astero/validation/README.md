# Acoustic validation

Run one requested voice turn using the local service in on-demand mode:

```sh
python -m astero.validation --text 'What is two plus two?'
```

The wake TTS uses `--wake-style 'Speak English clearly and slowly.'` by default. This makes the isolated name's synthetic English pronunciation explicit. It changes only the wake stimulus; the question keeps its normal voice/style behavior. Results include the exact `wake_phrase` and `wake_style` for comparison.

Use `--wake-style ''` to reproduce plain TTS without guidance. Improving this synthetic stimulus does not establish human wake-word accuracy. The CLI submits a new turn; do not use it to benchmark an already active resident listener without coordinating capture ownership.
