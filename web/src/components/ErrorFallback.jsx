import { useNavigate } from "react-router-dom";
import { RoutePaths } from "../general/RoutePaths.jsx";

export const ErrorFallback = ({ error, resetErrorBoundary }) => {
  const navigate = useNavigate();

  return (
    <div role="alert">
      <div className="bg-white pl-10 pt-10">
        <p>Something went wrong:</p>
        <pre style={{whiteSpace:'pre-wrap'}}>{String(error?.message || error)}</pre>
        <button onClick={resetErrorBoundary}>Try again</button>
      </div>
      <div className="mb-4 mt-16 flex w-full flex-col items-center justify-center space-y-16">
        <div>
          <a
            onClick={() => {
              resetErrorBoundary();
              navigate(RoutePaths.HOME);
            }}
          >
            Home
          </a>
        </div>
        <div>An error happened. Contact support please!</div>
      </div>
    </div>
  );
};
