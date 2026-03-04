import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

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
  const url = new URL(req.url);
  const itemId = url.searchParams.get("item");
  const uid = url.searchParams.get("uid");

  if (!itemId || !uid) {
    return new Response("Missing parameters", { status: 400 });
  }

  const db = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  );

  // Verify payment / access
  const { data: access } = await db
    .from("content_access")
    .select("item_id")
    .eq("x_user_id", uid)
    .eq("item_id", itemId)
    .single();

  if (!access) {
    return new Response("Payment required", { status: 402 });
  }

  // Get content item
  const { data: item } = await db
    .from("content_items")
    .select("*")
    .eq("id", itemId)
    .single();

  if (!item) {
    return new Response("Not found", { status: 404 });
  }

  const expiry = Math.floor(Date.now() / 1000) + 3600;
  let signedUrl: string;

  if (item.type === "video") {
    const token = await sha256Hex(`${BUNNY_STREAM_KEY}${item.bunny_path}${expiry}`);
    signedUrl = `https://iframe.mediadelivery.net/embed/${BUNNY_LIBRARY_ID}/${item.bunny_path}?token=${token}&expires=${expiry}`;
  } else {
    const token = await sha256Hex(`${BUNNY_SIGNING_KEY}${item.bunny_path}${expiry}`);
    signedUrl = `https://${BUNNY_CDN_HOSTNAME}${item.bunny_path}?token=${token}&expires=${expiry}`;
  }

  return Response.redirect(signedUrl, 302);
});
