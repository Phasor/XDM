export interface DM {
  id: string;
  fromUserId: string; // X conversation ID (e.g. "123456-789012") used as stable user key
  text: string;
  createdAt: Date;
}

export interface XAdapter {
  getNewDMs(since: Date): Promise<DM[]>;
  sendDM(toUserId: string, text: string): Promise<void>;
}

type SnapshotLine = { ref: string; kind: string; label: string };

export class PinchTabAdapter implements XAdapter {
  // Default port is 9867 — update PINCHTAB_URL in your .env if different
  private readonly baseUrl: string;
  private tabId: string | null = null;

  constructor() {
    this.baseUrl = process.env.PINCHTAB_URL ?? "http://localhost:9867";
  }

  // ------------------------------------------------------------------ low-level

  private async api(method: string, path: string, body?: unknown): Promise<unknown> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Id": "xdm-agent",
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`PinchTab ${method} ${path} → ${res.status}: ${text}`);
    }
    const ct = res.headers.get("content-type") ?? "";
    return ct.includes("application/json") ? res.json() : res.text();
  }

  /** Find or open the persistent DM tab. Re-uses across poll cycles. */
  private async ensureTab(): Promise<string> {
    if (this.tabId) return this.tabId;

    // When PinchTab runs as a daemon it starts Chrome directly, so /instances
    // returns []. Use GET /tabs to grab the existing tab instead.
    // Response shape: {"tabs": [...]} not a flat array.
    const tabsRes = (await this.api("GET", "/tabs")) as
      | { tabs: Array<{ id: string; url: string }> }
      | Array<{ id: string; url: string }>;
    const tabs = Array.isArray(tabsRes) ? tabsRes : (tabsRes.tabs ?? []);

    if (tabs.length > 0) {
      this.tabId = tabs[0].id;
      return this.tabId;
    }

    // Fallback: no tabs yet — start an instance from the first available profile.
    const profiles = (await this.api("GET", "/profiles")) as Array<{
      id: string;
      name: string;
    }>;
    if (profiles.length === 0) {
      throw new Error("No PinchTab profiles found. Create one and log into X first.");
    }
    const instance = (await this.api("POST", "/instances/start", {
      profileId: profiles[0].id,
      mode: "headless",
    })) as { id: string };
    await sleep(4000); // wait for Chrome to be ready
    const tab = (await this.api("POST", `/instances/${instance.id}/tabs/open`, {
      url: "https://x.com/messages",
    })) as { tabId: string };
    this.tabId = tab.tabId;
    return this.tabId;
  }

  private async navigate(url: string, waitMs = 2500): Promise<void> {
    const tabId = await this.ensureTab();
    await this.api("POST", `/tabs/${tabId}/navigate`, { url, timeout: 20 });
    await sleep(waitMs);
  }

  private async evaluate<T>(expression: string): Promise<T> {
    const tabId = await this.ensureTab();
    const result = await this.api("POST", `/tabs/${tabId}/evaluate`, { expression });
    return result as T;
  }

  private async snapshot(): Promise<SnapshotLine[]> {
    const tabId = await this.ensureTab();
    const raw = (await this.api(
      "GET",
      `/tabs/${tabId}/snapshot?interactive=true&compact=true`
    )) as string;
    return raw
      .split("\n")
      .map((line) => {
        const m = line.match(/^(e\d+):(\w+)\s+"?([^"]*)"?/);
        return m ? { ref: m[1], kind: m[2], label: m[3] } : null;
      })
      .filter(Boolean) as SnapshotLine[];
  }

  private async action(
    kind: string,
    ref: string,
    text?: string,
    key?: string
  ): Promise<void> {
    const tabId = await this.ensureTab();
    await this.api("POST", `/tabs/${tabId}/action`, { kind, ref, text, key });
  }

  private findRef(
    lines: SnapshotLine[],
    label: string | RegExp,
    kind?: string
  ): string | null {
    const test =
      typeof label === "string"
        ? (s: string) => s.toLowerCase().includes(label.toLowerCase())
        : (s: string) => label.test(s);
    return lines.find((l) => test(l.label) && (!kind || l.kind === kind))?.ref ?? null;
  }

  // ------------------------------------------------------------------ DM logic

  async getNewDMs(since: Date): Promise<DM[]> {
    await this.navigate("https://x.com/messages", 3000);

    // Extract conversation list — each item has a /messages/{conversationId} link
    // and a <time datetime="..."> showing when the last message was sent.
    type ConvInfo = { href: string; latestTime: string };
    const conversations = await this.evaluate<ConvInfo[]>(`
      Array.from(document.querySelectorAll('[data-testid="conversation"]')).map(el => {
        const link = el.querySelector('a[href*="/messages/"]');
        const time = el.querySelector('time');
        return {
          href: link ? 'https://x.com' + new URL(link.href).pathname : '',
          latestTime: time?.getAttribute('datetime') ?? '',
        };
      }).filter(c => c.href && c.latestTime)
    `);

    const dms: DM[] = [];

    for (const conv of conversations) {
      if (!conv.latestTime || new Date(conv.latestTime) <= since) continue;

      // The conversation ID is the path segment after /messages/
      // For 1-on-1 DMs it looks like "smallerUserId-largerUserId"
      const conversationId = conv.href.split("/messages/")[1];
      if (!conversationId) continue;

      await this.navigate(conv.href, 2000);

      // Extract incoming messages from the open conversation thread.
      // X marks outgoing messages with data-testid="sent-message" on an ancestor.
      // We read all messages and filter to incoming ones newer than `since`.
      type MsgInfo = { text: string; sentAt: string; isOutgoing: boolean };
      const messages = await this.evaluate<MsgInfo[]>(`
        (() => {
          const results = [];
          document.querySelectorAll('[data-testid="messageEntry"]').forEach(el => {
            const timeEl = el.querySelector('time');
            if (!timeEl) return;
            const sentAt = timeEl.getAttribute('datetime') ?? '';
            const isOutgoing = !!el.closest('[data-testid="sent-message"]');
            const textEl =
              el.querySelector('[data-testid="tweetText"]') ??
              el.querySelector('div[dir="auto"][lang]') ??
              el.querySelector('div[dir="auto"]');
            const text = textEl?.innerText?.trim() ?? '';
            if (text) results.push({ text, sentAt, isOutgoing });
          });
          return results;
        })()
      `);

      for (const msg of messages) {
        if (msg.isOutgoing || !msg.sentAt) continue;
        const msgDate = new Date(msg.sentAt);
        if (msgDate <= since) continue;
        dms.push({
          id: `${conversationId}::${msg.sentAt}`,
          fromUserId: conversationId,
          text: msg.text,
          createdAt: msgDate,
        });
      }
    }

    return dms;
  }

  async sendDM(toUserId: string, text: string): Promise<void> {
    // toUserId is a conversation ID ("123-456") or a bare numeric X user ID.
    // If it contains a hyphen we can navigate directly to the existing conversation.
    // Otherwise we use the compose URL to start/find a DM thread.
    const url = toUserId.includes("-")
      ? `https://x.com/messages/${toUserId}`
      : `https://x.com/messages/compose?recipient_id=${toUserId}`;

    await this.navigate(url, 2500);

    const lines = await this.snapshot();

    // X's DM compose box is a contenteditable / textbox.
    // Typical snapshot labels: "New message", "Start a new message", or just "textbox".
    const inputRef =
      this.findRef(lines, /new message|start a new/i, "textbox") ??
      this.findRef(lines, "textbox") ??
      this.findRef(lines, /message/i, "textbox");

    if (!inputRef) {
      throw new Error(
        `Could not find DM input in snapshot. Labels found: ${lines.map((l) => l.label).join(", ")}`
      );
    }

    await this.action("click", inputRef);
    await this.action("type", inputRef, text);

    // Try clicking a labelled Send button; fall back to Enter key.
    const sendRef = this.findRef(lines, /^send$/i, "button");
    if (sendRef) {
      await this.action("click", sendRef);
    } else {
      await this.action("press", inputRef, undefined, "Enter");
    }
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
