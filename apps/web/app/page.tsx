import LandingPage from "../components/landing/LandingPage";

// Always the public marketing page, logged in or not — LandingPage
// itself reads auth state to swap CTAs ("Start free trial" -> "Go to
// dashboard"), same pattern real SaaS marketing sites use rather than
// redirecting an existing customer away from their own homepage.
export default function Home() {
  return <LandingPage />;
}
