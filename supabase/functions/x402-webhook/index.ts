import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

Deno.serve(async (req: Request) => {
  const body = await req.json() as {
    tx_hash: string;
    extra: { uid: string; itemType: string; itemId: string };
    amount: string;
  };

  const db = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  );

  const { uid, itemType, itemId } = body.extra;

  await db.from("payments").insert({
    id: body.tx_hash,
    x_user_id: uid,
    item_type: itemType,
    item_id: itemId,
    usdc_amount: Number(body.amount) / 1_000_000,
    tx_hash: body.tx_hash,
    status: "confirmed",
    confirmed_at: new Date().toISOString(),
  });

  if (itemType === "chat") {
    const { data: config } = await db
      .from("agent_config")
      .select("chat_session_hours")
      .eq("id", "default")
      .single();
    const hours = Number(config?.chat_session_hours ?? 24);
    const until = new Date(Date.now() + hours * 3_600_000).toISOString();
    await db.from("x_users").update({ chat_access_until: until }).eq("id", uid);
  } else {
    await db.from("content_access").upsert({ x_user_id: uid, item_id: itemId });
  }

  return new Response(JSON.stringify({ ok: true }), {
    headers: { "Content-Type": "application/json" },
  });
});
