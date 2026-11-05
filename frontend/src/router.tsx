import { Route, Switch, Redirect, useParams, useSearch } from "wouter";
import { LoginPage } from "@/components/pages/LoginPage";
import { LoadingState } from "@/components/shared/LoadingState";
import { LeaveGuardProvider } from "@/components/shared/edit-unit/LeaveGuard";
import { ToastOverlay } from "@/components/layout/ToastOverlay";
import { useAuthStore } from "@/stores/auth-store";
import { ProductionPage } from "@/components/production/ProductionPage";
import { ProjectsPage } from "@/components/production/management/ProjectsPage";
import { PRODUCTION_PATH, PROJECTS_PATH } from "@/components/production/production-paths";
import { SystemConfigPage } from "@/components/pages/SystemConfigPage";

function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuthStore();
  if (isLoading) return <LoadingState variant="screen" />;
  if (!isAuthenticated) return <Redirect to={`/login?from=${encodeURIComponent(window.location.pathname + window.location.search)}`} />;
  return children;
}

function WorkRoute() {
  const { projectName } = useParams<{ projectName?: string }>();
  const search = useSearch();
  const episode = new URLSearchParams(search).get("episode") ?? "legacy";
  return <ProductionPage key={`${projectName ?? "new"}/${episode}`} />;
}

export function AppRoutes() {
  return (
    <LeaveGuardProvider>
      <Switch>
        <Route path="/login" component={LoginPage} />
        <Route path="/app/settings"><AuthGuard><SystemConfigPage /></AuthGuard></Route>
        <Route path={`${PROJECTS_PATH}/:projectName`}><AuthGuard><ProjectsPage /></AuthGuard></Route>
        <Route path={PROJECTS_PATH}><AuthGuard><ProjectsPage /></AuthGuard></Route>
        <Route path={`${PRODUCTION_PATH}/:projectName`}><AuthGuard><WorkRoute /></AuthGuard></Route>
        <Route path={PRODUCTION_PATH}><AuthGuard><WorkRoute /></AuthGuard></Route>
        <Route><Redirect to={PRODUCTION_PATH} /></Route>
      </Switch>
      <ToastOverlay />
    </LeaveGuardProvider>
  );
}
