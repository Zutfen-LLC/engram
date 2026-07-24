import { PageContainer, StatusCard, type StatusTone } from "engram-ui";
import { getControlPlaneClientSafe } from "@/lib/control-plane";

/**
 * Portal home — the hosted-platform status scaffold (ENG-PORTAL-001A).
 *
 * This is a SERVER component. It fetches Control Plane metadata server-side via
 * the typed portal-sdk; the internal Control Plane URL never reaches the
 * browser. The page honestly labels the app as scaffolding and renders no fake
 * login, signup, billing, or agent-management actions.
 *
 * Status is conveyed by text + symbol (never color alone).
 */
export const dynamic = "force-dynamic";

async function fetchControlPlaneStatus(): Promise<{
  reachable: boolean;
  meta?: Record<string, string>;
}> {
  try {
    const client = getControlPlaneClientSafe();
    if (!client) {
      return { reachable: false };
    }
    const meta = await client.getMeta();
    // Only safe metadata fields are surfaced; no internal addresses.
    return {
      reachable: true,
      meta: {
        database_boundary: meta.database_boundary,
        identity_status: meta.identity_status,
        delegation_status: meta.delegation_status,
        billing_status: meta.billing_status,
        core_access_mode: meta.core_access_mode,
        environment: meta.environment,
      },
    };
  } catch {
    return { reachable: false };
  }
}

export default async function HomePage() {
  const status = await fetchControlPlaneStatus();
  const cpTone: StatusTone = status.reachable ? "ok" : "err";

  return (
    <PageContainer
      header={
        <header
          style={{
            borderBottom: "1px solid #d0d7de",
            padding: "8px 16px",
          }}
        >
          <strong>Engram Portal</strong>
        </header>
      }
      footer={
        <footer style={{ borderTop: "1px solid #d0d7de", padding: "8px 16px" }}>
          <span style={{ color: "#656d76" }}>
            Hosted-platform scaffolding — not a production management console.
          </span>
        </footer>
      }
    >
      <h1>Engram Portal</h1>
      <p role="note" style={{ background: "#fff8c5", border: "1px solid #d4a72c", padding: "12px", borderRadius: "8px" }}>
        This is hosted-platform <strong>scaffolding</strong>, not a production
        management console. No login, signup, billing, or agent-management
        functionality is available yet.
      </p>

      <section aria-label="Service status" style={{ display: "grid", gap: "16px", marginTop: "24px" }}>
        <StatusCard title="Portal" tone="ok" detail="hosted scaffold running" />
        <StatusCard
          title="Control Plane"
          tone={cpTone}
          detail={status.reachable ? "reachable" : "unreachable"}
        />
        <StatusCard
          title="Identity"
          tone="unknown"
          label="not configured"
          detail="CIAM adapter is a future slice (ENG-IDENTITY-001)."
        />
        <StatusCard
          title="Core Delegation"
          tone="unknown"
          label="not implemented"
          detail="Short-lived delegated authorization is a future slice (ENG-DELEGATION-001)."
        />
        <StatusCard
          title="Billing"
          tone="unknown"
          label="not configured"
          detail="Stripe / entitlements are a future slice."
        />
      </section>
    </PageContainer>
  );
}
