// Inherited-pipe framing only. No HTTP server, SDK patch, protocol filtering,
// Runtime subscription or payload logging. Chromium reads fd 3 and writes fd 4.
'use strict';
const {spawn} = require('node:child_process');
const {Transform, pipeline} = require('node:stream');
const MAX = 8000000;

class Frames extends Transform {
  constructor(input, output) { super(); this.input = input; this.output = output; this.pending = Buffer.alloc(0); }
  _transform(chunk, _encoding, done) {
    try {
      this.pending = Buffer.concat([this.pending, chunk]);
      for (;;) {
        const end = this.pending.indexOf(this.input);
        if (end === -1) break;
        if (end > MAX) throw new Error();
        const frame = Buffer.from(this.pending.subarray(0, end + 1));
        frame[end] = this.output;
        this.pending = this.pending.subarray(end + 1);
        this.push(frame);
      }
      if (this.pending.length > MAX) throw new Error();
      done();
    } catch { done(new Error('pipe framing failed')); }
  }
}

let child;
const stop = () => { if (child && child.exitCode === null && child.signalCode === null) child.kill(); };
try {
  const [executable, serialized] = process.argv.slice(2);
  const args = JSON.parse(serialized);
  if (!executable || !Array.isArray(args) || args.length > 64 || args.some(x => typeof x !== 'string' || x.length > 8192)) throw new Error();
  if (!args.includes('--remote-debugging-pipe') || args.some(x => x.startsWith('--remote-debugging-port'))) throw new Error();
  child = spawn(executable, args, {stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'], windowsHide: true});
  child.on('error', () => { process.exitCode = 1; stop(); process.stdin.destroy(); });
  child.on('close', code => { process.exitCode = code === 0 ? 0 : 1; process.stdin.destroy(); });
  process.stdin.on('end', stop);
  const ended = error => {
    // Browser.close can close its debugging pipe before profile cleanup has
    // finished. Allow that owned process to exit, then bound a real stall.
    if (error && child.exitCode !== 0) { process.exitCode = 1; setTimeout(stop, 5000).unref(); }
  };
  pipeline(process.stdin, new Frames(10, 0), child.stdio[3], ended);
  pipeline(child.stdio[4], new Frames(0, 10), process.stdout, ended);
} catch { process.exitCode = 1; stop(); }
