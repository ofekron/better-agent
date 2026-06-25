import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  componentStack: string | null;
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null,
    componentStack: null,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error, componentStack: null };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("Uncaught error:", error, errorInfo);
    this.setState({ componentStack: errorInfo.componentStack ?? null });
  }

  public render() {
    if (this.state.hasError) {
      return (
        <div style={{
          background: "#0d1117",
          height: "100vh",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          color: "#8b949e",
          fontFamily: "sans-serif",
          padding: "20px",
          textAlign: "center"
        }}>
          <h1>Something went wrong.</h1>
          <p>{this.state.error?.message}</p>
          {this.state.componentStack && (
            <details style={{ maxWidth: "90vw", marginTop: "12px" }}>
              <summary style={{ cursor: "pointer" }}>Component stack</summary>
              <pre style={{
                textAlign: "left",
                maxHeight: "40vh",
                overflow: "auto",
                fontSize: "12px",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}>
                {this.state.componentStack}
              </pre>
            </details>
          )}
          <button
            onClick={() => window.location.reload()}
            style={{
              marginTop: "20px",
              padding: "10px 20px",
              background: "#3fb950",
              color: "white",
              border: "none",
              borderRadius: "6px",
              cursor: "pointer"
            }}
          >
            Reload App
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
