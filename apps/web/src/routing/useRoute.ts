import { useEffect, useState } from 'react'

import { navigate, parseHash, type Route } from './route'

/** Tracks the URL hash as a `Route`, updating on the browser's own `hashchange` event so back /
 * forward / a pasted link all work exactly as they do in the vanilla UI. */
export function useRoute(): [Route, typeof navigate] {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash))

  useEffect(() => {
    function onHashChange() {
      setRoute(parseHash(window.location.hash))
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  return [route, navigate]
}
