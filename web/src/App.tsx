import type { ApiClient } from "./api/client";
import { DesktopApp } from "./desktop/DesktopApp";
import { PhoneApp } from "./phone/PhoneApp";
import { HubProvider } from "./state/hub";
import { UiProvider } from "./state/ui";
import { useMediaQuery } from "./state/useMediaQuery";

/**
 * Below 1024 px the three panes do not fit, so phones and portrait tablets
 * get the agent-first layout (DESIGN.md section 14 allows either for tablets).
 */
const PHONE_QUERY = "(max-width: 1023.98px)";

function Layout() {
  const phone = useMediaQuery(PHONE_QUERY);
  return phone ? <PhoneApp /> : <DesktopApp />;
}

export function App({ client, mock }: { client: ApiClient; mock: boolean }) {
  return (
    <HubProvider client={client} mock={mock}>
      <UiProvider>
        <Layout />
      </UiProvider>
    </HubProvider>
  );
}
