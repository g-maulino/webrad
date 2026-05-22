# ts_inject

This tool provides a TS segment modification feature to add ID3 tags with Artist and Song name provided

---

## Build


```bash
gcc -O2 -Wall -o ts_inject ts_inject.c
```

To be used with supervisor.py. Sets the ts_inject binary path in the supervisor CFG.

---

## Usage

```bash
ts_inject --input FILE.ts --title TITRE [--artist ARTISTE] [--pmt-pid PID]
  --input   FILE.ts    MPEG-TS segment to update (in-place)
  --title   TITRE      Song title (UTF-8)
  --artist  ARTISTE    Artist name (optional, UTF-8)
  --pmt-pid PID        PMT PID in decimal (defaut: 4096 = 0x1000)
```

## License

MIT
