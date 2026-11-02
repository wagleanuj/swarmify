// The install step's ffmpeg and ffprobe (ADR 0026), run as `npm run ffmpeg`: ffmpeg-static's
// release builds, fetched here because its own install script drops the sandbox proxy's
// credentials (HTTP 407), and @ffmpeg-installer's script-free builds are ffmpeg 4, older than the
// 5.1 HyperFrames' `-fps_mode` needs. Each download must match its pinned SHA-256.
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

const RELEASE = "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1";
const SHA256 = {
  "ffmpeg-darwin-arm64": "8923876afa8db5585022d7860ec7e589af192f441c56793971276d450ed3bbfa",
  "ffmpeg-darwin-x64": "929b375c1182d956c51f7ac25e0b2b0411fb01f6f407aa15c9758efeb4242106",
  "ffmpeg-linux-arm64": "754a678672298bc68156adff58aa7385a592c2b30b1d0ae8750c45c915c4bac0",
  "ffmpeg-linux-x64": "bfe8a8fc511530457b528c48d77b5737527b504a3797a9bc4866aeca69c2dffa",
  "ffprobe-darwin-arm64": "d986a8ec7b030899fe66a8a288ed809a3543338705a3ce178cfb85869c5d80be",
  "ffprobe-darwin-x64": "d4da574d6e2e197bd259b47d69cf262df9e312af24ad960444f6d806d3d4c186",
  "ffprobe-linux-arm64": "2ab6aba60ee84412dff9188720703376cb4e7aaf7e0b5e43aa8249f2acae5bf8",
  "ffprobe-linux-x64": "25d9b6ccb05e3d9de9e04e31e2506d8dd7f9f0418981965ac6df12e8d3afd067",
};

mkdirSync("node_modules/.ffmpeg", { recursive: true });
for (const tool of ["ffmpeg", "ffprobe"]) {
  const name = `${tool}-${process.platform}-${process.arch}`;
  if (!SHA256[name]) throw new Error(`no ${tool} build for ${process.platform} ${process.arch}`);
  const got = await fetch(`${RELEASE}/${name}.gz`);
  if (!got.ok) throw new Error(`${RELEASE}/${name}.gz: HTTP ${got.status}`);
  const gz = Buffer.from(await got.arrayBuffer());
  const sha = createHash("sha256").update(gz).digest("hex");
  if (sha !== SHA256[name]) throw new Error(`${name}.gz has SHA-256 ${sha}, not the pinned one`);
  writeFileSync(`node_modules/.ffmpeg/${tool}`, gunzipSync(gz), { mode: 0o755 });
}
