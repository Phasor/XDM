import { createHash } from "crypto";

const BUNNY_SIGNING_KEY = process.env.BUNNY_SIGNING_KEY ?? "";
const BUNNY_CDN_HOSTNAME = process.env.BUNNY_CDN_HOSTNAME ?? "";
const BUNNY_STREAM_KEY = process.env.BUNNY_STREAM_API_KEY ?? "";
const BUNNY_LIBRARY_ID = process.env.BUNNY_STREAM_LIBRARY_ID ?? "";

export function signedStorageUrl(path: string, expirySeconds = 3600): string {
  const expiry = Math.floor(Date.now() / 1000) + expirySeconds;
  const token = createHash("sha256")
    .update(`${BUNNY_SIGNING_KEY}${path}${expiry}`)
    .digest("hex");
  return `https://${BUNNY_CDN_HOSTNAME}${path}?token=${token}&expires=${expiry}`;
}

export function signedStreamUrl(videoId: string, expirySeconds = 3600): string {
  const expiry = Math.floor(Date.now() / 1000) + expirySeconds;
  const token = createHash("sha256")
    .update(`${BUNNY_STREAM_KEY}${videoId}${expiry}`)
    .digest("hex");
  return `https://iframe.mediadelivery.net/embed/${BUNNY_LIBRARY_ID}/${videoId}?token=${token}&expires=${expiry}`;
}
