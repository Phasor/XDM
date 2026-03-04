import { signedStorageUrl, signedStreamUrl } from "../payments/bunny.js";
import type { getSupabaseClient } from "../db/supabase.js";

export const TOOLS = [
  {
    type: "function",
    function: {
      name: "deliver_content",
      description: "Send a signed download/stream link for a content item",
      parameters: {
        type: "object",
        properties: {
          item_id: { type: "string" },
        },
        required: ["item_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "generate_payment_link",
      description: "Create an X402 payment page URL for content or chat access",
      parameters: {
        type: "object",
        properties: {
          item_type: { type: "string", enum: ["content", "chat"] },
          item_id: {
            type: "string",
            description: "content item ID, or 'session' for chat",
          },
        },
        required: ["item_type", "item_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "check_payment",
      description: "Check whether user has paid for a specific item",
      parameters: {
        type: "object",
        properties: {
          item_type: { type: "string", enum: ["content", "chat"] },
          item_id: { type: "string" },
        },
        required: ["item_type", "item_id"],
      },
    },
  },
];

type ToolCall = { name: string; arguments: Record<string, string> };
type Adapter = { sendDM: (userId: string, text: string) => Promise<void> };
type DB = ReturnType<typeof getSupabaseClient>;

export async function executeToolCall(
  tc: ToolCall,
  userId: string,
  adapter: Adapter,
  db: DB
): Promise<void> {
  switch (tc.name) {
    case "deliver_content": {
      const { data: item } = await db
        .from("content_items")
        .select("*")
        .eq("id", tc.arguments.item_id)
        .single();

      if (!item) {
        await adapter.sendDM(userId, "Sorry, that content item was not found.");
        return;
      }

      const url =
        item.type === "video"
          ? signedStreamUrl(item.bunny_path)
          : signedStorageUrl(item.bunny_path);

      await adapter.sendDM(userId, `Here's your link (expires in 1 hour): ${url}`);
      break;
    }

    case "generate_payment_link": {
      const base = process.env.VPS_BASE_URL ?? "https://yourdomain.com";
      const link = `${base}/pay/${tc.arguments.item_type}/${tc.arguments.item_id}?uid=${userId}`;
      await adapter.sendDM(userId, `To access this, please complete payment here: ${link}`);
      break;
    }

    case "check_payment": {
      if (tc.arguments.item_type === "content") {
        const { data } = await db
          .from("content_access")
          .select("item_id")
          .eq("x_user_id", userId)
          .eq("item_id", tc.arguments.item_id)
          .single();
        console.log(`check_payment content ${tc.arguments.item_id} for ${userId}: ${data ? "granted" : "not granted"}`);
      } else {
        const { data: user } = await db
          .from("x_users")
          .select("chat_access_until")
          .eq("id", userId)
          .single();
        const active =
          user?.chat_access_until && new Date(user.chat_access_until) > new Date();
        console.log(`check_payment chat for ${userId}: ${active ? "active" : "inactive"}`);
      }
      break;
    }
  }
}
