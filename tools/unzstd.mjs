import { readFileSync } from 'node:fs'
import { zstdDecompressSync } from 'node:zlib'
process.stdout.write(zstdDecompressSync(readFileSync(process.argv[2])))
