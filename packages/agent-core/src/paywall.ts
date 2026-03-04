interface User {
  free_messages_used: number;
  chat_access_until: string | null;
}

interface PaywallResult {
  allowed: boolean;
  paymentPrompt?: string;
}

export function checkPaywall(
  user: User,
  config: Record<string, unknown>
): PaywallResult {
  const freeLimit = (config.free_message_limit as number) ?? 3;
  const sessionPrice = config.chat_session_price_usd as number;
  const sessionHours = (config.chat_session_hours as number) ?? 24;

  // Still within free quota
  if (user.free_messages_used < freeLimit) {
    return { allowed: true };
  }

  // Active paid session
  if (
    user.chat_access_until &&
    new Date(user.chat_access_until) > new Date()
  ) {
    return { allowed: true };
  }

  // Paywall hit — build payment prompt
  const base = process.env.VPS_BASE_URL ?? "https://yourdomain.com";
  const paymentPrompt =
    `Hey! You've used your ${freeLimit} free messages. ` +
    `To keep chatting, unlock ${sessionHours}h access for $${sessionPrice} USDC:\n` +
    `${base}/pay/chat/session?uid=<USER_ID>`;

  return { allowed: false, paymentPrompt };
}
