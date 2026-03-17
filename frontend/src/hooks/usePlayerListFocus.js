import { useRef, useState, useEffect } from 'react'

/**
 * Manages focus-advancing behaviour for a list of PlayerSearch inputs.
 *
 * When the user presses Enter in slot i:
 *   - If slot i+1 already exists, focus it immediately.
 *   - Otherwise call addPlayer() to create a new slot, then focus it
 *     once React has rendered it.
 *
 * Usage in a parent component:
 *
 *   const { playerRefs, focusNextOrAdd } = usePlayerListFocus(players, addPlayer)
 *
 *   // In JSX:
 *   <PlayerSearch
 *     ref={el => { playerRefs.current[idx] = el }}
 *     onEnterKey={() => focusNextOrAdd(idx)}
 *     ...
 *   />
 */
export function usePlayerListFocus(players, addPlayer) {
  const playerRefs    = useRef([])
  const [pendingIdx, setPendingIdx] = useState(null)

  // Fire focus once the new slot has mounted
  useEffect(() => {
    if (pendingIdx !== null) {
      const ref = playerRefs.current[pendingIdx]
      if (ref) {
        ref.focus()
        setPendingIdx(null)
      }
    }
  }, [players.length, pendingIdx])

  function focusNextOrAdd(currentIdx) {
    const nextRef = playerRefs.current[currentIdx + 1]
    if (nextRef) {
      nextRef.focus()
    } else {
      addPlayer()
      setPendingIdx(currentIdx + 1)
    }
  }

  return { playerRefs, focusNextOrAdd }
}
