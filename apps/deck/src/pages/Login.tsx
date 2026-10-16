import { useState } from "react";
import { loginWithPasskey, registerPasskey } from "../api";

export function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const attempt = (action: () => Promise<void>, failure: string): void => {
    setBusy(true);
    setMessage("");
    action()
      .then(onSignedIn)
      .catch(() => {
        setMessage(failure);
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <div className="login">
      <h1>JARVIS</h1>
      <button
        type="button"
        className="primary"
        disabled={busy}
        onClick={() => {
          attempt(loginWithPasskey, "Anmeldung fehlgeschlagen.");
        }}
      >
        Mit Passkey anmelden
      </button>
      <details>
        <summary>Erster Start: Passkey einrichten</summary>
        <label htmlFor="code">Einrichtungscode</label>
        <input
          id="code"
          autoComplete="one-time-code"
          value={code}
          onChange={(e) => {
            setCode(e.target.value);
          }}
        />
        <button
          type="button"
          disabled={busy || code.length === 0}
          onClick={() => {
            attempt(async () => {
              await registerPasskey(code);
              await loginWithPasskey();
            }, "Einrichtung fehlgeschlagen. Ist der Code richtig?");
          }}
        >
          Passkey einrichten
        </button>
      </details>
      <p role="status" className="message">
        {message}
      </p>
    </div>
  );
}
