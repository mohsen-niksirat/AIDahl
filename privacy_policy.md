# Privacy Policy for Dahl AI Assistant Bot (@MyAIDahlAssistantBot)

**Last Updated:** September 20, 2026

Welcome to Dahl AI Assistant Bot ("the Bot", "we", "our"). We are committed to protecting your privacy and ensuring transparency in how information is handled.

---

## 1. Information We Collect

To provide core conversational functionality, the Bot processes and stores the following technical information:

- **Telegram Profile Data:** Your unique Telegram User ID, first name, and username (if set).
- **Messages & Conversations:** User prompts and assistant responses for your currently active conversation session to maintain multi-turn chat context.
- **API Keys (BYOK - Bring Your Own Key):** If you register personal API keys (such as Dahl, Groq, OpenRouter, Mistral, or Manus), these are stored in your private database record solely to route model inference requests directly on your behalf.
- **Usage Statistics:** The count of input and output tokens consumed per session to calculate daily and lifetime quotas.

---

## 2. How Your Information is Used

We use the collected information exclusively to:
- Authenticate and manage your chat sessions inside Telegram.
- Forward conversational prompts to your designated AI inference provider (Dahl, Groq, OpenRouter, Manus, etc.).
- Maintain multi-turn conversational memory within active sessions.
- Enforce fair-use token quotas.

---

## 3. Data Retention & Hygiene Policy

We operate under a strict minimal-data hygiene policy:
- **Active Chat Storage:** Only messages belonging to your active conversation are retained on the backend.
- **Session Reset (`/clear`):** Initiating a new conversation clears previous server-side message history for that context.
- **Local History:** Your full visual chat history remains stored securely on your own device within the Telegram application.
- **No Third-Party Reselling:** We do not sell, rent, or trade your personal data or conversation content.

---

## 4. Third-Party AI Services

When you interact with the Bot, your prompts are sent to external AI inference providers based on your selected model (e.g., Dahl Inference, OpenRouter, Groq, Manus). Interactions with these services are governed by their respective privacy policies and terms of service. When using BYOK (Bring Your Own Key), interactions occur directly against your own provider account quotas.

---

## 5. Security & BYOK Protection

- Your personal API keys are masked in the Telegram user interface.
- Database access is restricted to backend services using environment secrets.
- You can delete or update your registered API keys at any time using the `/keys` command.

---

## 6. User Rights & Data Deletion

You retain full control over your data:
- Use `/clear` to wipe active server conversation context.
- Use `/keys` to revoke or delete any stored API credentials.
- Contact the administrator via the GitHub repository (`https://github.com/mohsen-niksirat/AIDahl`) to request complete account data deletion.

---

## 7. Changes to This Policy

We may update this Privacy Policy from time to time to reflect new features or model integrations. Material changes will be noted via Bot release updates.
