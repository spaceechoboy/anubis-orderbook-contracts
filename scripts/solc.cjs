// Standard JSON only; unlike the CLI this emits no diagnostic banner on stdout.
const fs = require('node:fs');
const solc = require('solc');
if (!solc.version().startsWith('0.8.24+commit.e11b9ed9')) throw new Error('solc 0.8.24 required');
process.stdout.write(solc.compile(fs.readFileSync(0, 'utf8')));
