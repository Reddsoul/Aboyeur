# Privacy Policy

**DRAFT — requires attorney review before launch.** Version 2026-09-19.

This policy explains what **[ENTITY NAME]** ("we") collects when you use Aboyeur, why, and what you can do about it. It is written to meet GDPR-style expectations even though the Service is hosted in and offered from the United States.

## 1. What we collect

| Category | Examples | Source |
|---|---|---|
| Account data | Email address, password (hashed), name and practice (optional), country, time zone, terms-acceptance version | You |
| Billing data | Stripe customer and subscription identifiers, plan, payment status, prepaid credit balance, invoice history. **We never see or store your card number.** | You via Stripe; Stripe |
| Client profiles | Names, contact details and notes for the people you plan for; household members' age, sex, height, weight, activity, goals, allergies, dislikes, diet modes | You |
| Conversations | Messages you send to the assistant and its replies; menus and kits produced | You; generated |
| Usage metrics | Turn counts, tokens processed, model used, latency, error types, timestamps | Generated |
| Technical data | IP address (for rate limiting and, when behind our CDN, an approximate country), browser type, request logs | Automatic |
| Support access record | When an operator views a flagged conversation for support | Generated |

We do not collect precise location, biometrics, or data from third-party trackers.

## 2. Why we use it (purposes and legal bases)

| Purpose | Legal basis |
|---|---|
| Providing the Service you signed up for — accounts, chat, menus, billing | Performance of a contract |
| Sending your messages to the AI provider you selected (ours or your own) to generate responses | Performance of a contract |
| Fraud prevention, rate limiting, security monitoring, error tracking | Legitimate interest |
| Product analytics from aggregate usage metrics (never message content) | Legitimate interest |
| Transactional email: verification, password reset, low-balance and deletion notices | Performance of a contract |
| Automated classification of your messages for sensitive content, used only to redact them from operator view | Legitimate interest (protecting you) |
| Anything else, for example product announcements | Consent, which you can withdraw |

We do not sell personal information and we do not use your content to train AI models.

## 3. Subprocessors

| Provider | Purpose | Location | Link |
|---|---|---|---|
| Hetzner Online GmbH | Hosting (virtual server) | United States (Ashburn) | https://www.hetzner.com/legal/privacy-policy |
| Backblaze, Inc. | Encrypted database backups | United States | https://www.backblaze.com/company/privacy |
| Cloudflare, Inc. | DNS, TLS termination, DDoS protection | Global edge | https://www.cloudflare.com/privacypolicy/ |
| Stripe, Inc. | Payments, invoices, customer portal | United States | https://stripe.com/privacy |
| Resend, Inc. | Transactional email | United States | https://resend.com/legal/privacy-policy |
| AI model providers you select in Settings when using Aboyeur's hosted models (currently: **[LIST — e.g. OpenAI, Anthropic]**) | Generating assistant responses | United States | Shown in Settings |
| Sentry (Functional Software, Inc.) — optional | Error reporting; personal data is scrubbed before sending | United States | https://sentry.io/privacy/ |

If you bring your own API key, the provider you configure receives your messages directly under its own terms and is not our subprocessor.

## 4. Retention

| Data | Retained |
|---|---|
| Account and client profiles, conversations, menus | Until you delete them or your account |
| Deleted accounts | Soft-deleted immediately (inaccessible); permanently erased **30 days** later |
| Usage/turn events | 90 days |
| Prepaid-credit ledger | Life of the account (needed to reconcile your balance), then erased with the account |
| Stripe webhook records | 1 year |
| Operator audit log | 2 years |
| Server request logs | 14 days |
| Stripe customer record | Retained by Stripe per its legal obligations (tax/accounting) even after account deletion |

## 5. Security

- Client names, contact details, notes and household health profiles are **encrypted at rest** with a key separate from the one protecting stored API keys.
- All traffic uses TLS. Session cookies are HttpOnly, Secure and SameSite.
- Operator access to conversations is limited and **audited**: messages our classifier flags as sensitive are hidden from operators by default, and revealing one is logged and shown to you on your Account page.
- Passwords are hashed with bcrypt and checked against known-breach lists at signup.
- Backups are streamed encrypted to off-site storage.

No system is perfectly secure. If we learn of a breach affecting your personal data we will notify you without undue delay and in any case **within 72 hours** of confirming it.

## 6. Your rights

Wherever you are, you can:

- **Export** everything we hold about you as a zip of JSON files: Account → *Download my data* (`GET /api/account/export`).
- **Correct** your account and client data directly in the app.
- **Delete** your account: Account → *Delete account* (`DELETE /api/account`). Your subscription is cancelled at once; data is erased after 30 days.
- **Object** to processing based on legitimate interest, or **withdraw consent**, by contacting us.
- **Complain** to your local data-protection authority.

To exercise a right you cannot exercise in-app, email [CONTACT EMAIL]. We will respond within 30 days.

## 7. California residents (CCPA/CPRA)

We do not sell or share personal information for cross-context behavioural advertising, and we have not done so in the preceding 12 months. You have the rights to know, delete, correct and to non-discrimination described above.

## 8. Children

The Service is not directed at anyone under 18 and we do not knowingly collect their data. Client profiles you enter may describe household members of any age; you are responsible for having permission to enter them.

## 9. International transfers

The Service is hosted in the United States. If you use it from elsewhere, your data is transferred to and processed in the US. Where required, we rely on standard contractual clauses with our subprocessors.

## 10. Cookies

We use a single session cookie to keep you signed in, and browser local storage for interface preferences. **We do not use advertising or analytics trackers** (no Google Analytics, no pixels).

## 11. Changes

We will notify you of material changes by email or in-app and update the version date above.

## 12. Contact

[ENTITY NAME] · [POSTAL ADDRESS] · [CONTACT EMAIL]
