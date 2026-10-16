import { useCallback, useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { getMe } from "./api";
import { DASHBOARDS } from "./dashboards";
import { Home } from "./pages/Home";
import { Login } from "./pages/Login";
import { Placeholder } from "./pages/Placeholder";
import { Shell } from "./Shell";

type AuthState = "checking" | "signed-in" | "signed-out";

export function App() {
  const [auth, setAuth] = useState<AuthState>("checking");

  const check = useCallback(() => {
    getMe()
      .then(() => {
        setAuth("signed-in");
      })
      .catch(() => {
        setAuth("signed-out");
      });
  }, []);

  useEffect(check, [check]);

  if (auth === "checking") return <p className="boot">Verbinde mit JARVIS …</p>;
  if (auth === "signed-out") return <Login onSignedIn={check} />;
  return (
    <Routes>
      <Route element={<Shell onSignedOut={check} />}>
        <Route index element={<Home />} />
        {DASHBOARDS.filter((d) => d.path !== "/").map((d) => (
          <Route key={d.path} path={d.path.slice(1)} element={<Placeholder dashboard={d} />} />
        ))}
        <Route path="*" element={<p className="empty">Diese Ansicht gibt es nicht.</p>} />
      </Route>
    </Routes>
  );
}
