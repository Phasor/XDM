import type { DM, XAdapter } from "./pinchtab.js";

// Placeholder for future official Twitter API v2 implementation.
// Drop-in replacement for PinchTabAdapter once DM endpoints are available.
export class TwitterAPIAdapter implements XAdapter {
  async getNewDMs(_since: Date): Promise<DM[]> {
    throw new Error("TwitterAPIAdapter not implemented yet");
  }

  async sendDM(_toUserId: string, _text: string): Promise<void> {
    throw new Error("TwitterAPIAdapter not implemented yet");
  }
}
