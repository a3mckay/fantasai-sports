import { Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import Spinner from './Spinner'

/**
 * Wraps routes that require authentication.
 * - Loading  → spinner
 * - No user  → redirect to /login
 * - User but onboarding incomplete → redirect to /onboarding
 * - All good → render children
 */
export default function AuthGuard({ children }) {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Spinner />
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" replace />
  }

  if (!user.onboarding_complete) {
    return <Navigate to="/onboarding" replace />
  }

  return children
}
