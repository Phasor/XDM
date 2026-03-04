// Internal function — called by agent-core to generate signed Bunny.net URLs.

const BUNNY_SIGNING_KEY = Deno.env.get("BUNNY_SIGNING_KEY") ?? "";
const BUNNY_CDN_HOSTNAME = Deno.env.get("BUNNY_CDN_HOSTNAME") ?? "";
const BUNNY_STREAM_KEY = Deno.env.get("BUNNY_STREAM_API_KEY") ?? "";
const BUNNY_LIBRARY_ID = Deno.env.get("BUNNY_STREAM_LIBRARY_ID") ?? "";

async function sha256Hex(input: string): Promise<string> {
  const buf = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(input)
  );
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

Deno.serve(async (req: Request) => {
  const { path, type, expirySeconds = 3600 } = await req.json() as {
    path: string;
    type: "storage" | "stream";
    expirySeconds?: number;
  };

  const expiry = Math.floor(Date.now() / 1000) + expirySeconds;
  let url: string;

  if (type === "stream") {
    const token = await sha256Hex(`${BUNNY_STREAM_KEY}${path}${expiry}`);
    url = `https://iframe.mediadelivery.net/embed/${BUNNY_LIBRARY_ID}/${path}?token=${token}&expires=${expiry}`;
  } else {
    const token = await sha256Hex(`${BUNNY_SIGNING_KEY}${path}${expiry}`);
    url = `https://${BUNNY_CDN_HOSTNAME}${path}?token=${token}&expires=${expiry}`;
  }

  return new Response(JSON.stringify({ url }), {
    headers: { "Content-Type": "application/json" },
  });
});
