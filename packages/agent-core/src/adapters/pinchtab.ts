export interface DM {
  id: string;
  fromUserId: string;
  text: string;
  createdAt: Date;
}

export interface XAdapter {
  getNewDMs(since: Date): Promise<DM[]>;
  sendDM(toUserId: string, text: string): Promise<void>;
}

export class PinchTabAdapter implements XAdapter {
  private baseUrl: string;

  constructor() {
    this.baseUrl = process.env.PINCHTAB_URL ?? "http://localhost:9000";
  }

  async getNewDMs(since: Date): Promise<DM[]> {
    const res = await fetch(
      `${this.baseUrl}/dms?since=${since.toISOString()}`
    );
    if (!res.ok) throw new Error(`PinchTab getNewDMs error: ${res.status}`);
    const data = (await res.json()) as Array<{
      id: string;
      from_user_id: string;
      text: string;
      created_at: string;
    }>;
    return data.map((d) => ({
      id: d.id,
      fromUserId: d.from_user_id,
      text: d.text,
      createdAt: new Date(d.created_at),
    }));
  }

  async sendDM(toUserId: string, text: string): Promise<void> {
    const res = await fetch(`${this.baseUrl}/dms/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to_user_id: toUserId, text }),
    });
    if (!res.ok) throw new Error(`PinchTab sendDM error: ${res.status}`);
  }
}
