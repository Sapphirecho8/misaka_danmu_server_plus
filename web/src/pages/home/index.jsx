import { Logs } from './components/Logs'
import { SearchBar } from './components/SearchBar'
import { SearchResult } from './components/SearchResult'
import { Test } from './components/Test'
import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../store/index.js'

export const Home = () => {
  const userinfo = useAtomValue(userinfoAtom)
  const perms = userinfo?.permissions || {}
  const showLogs = (userinfo?.username || '').toLowerCase() === 'admin' || !!perms.viewHomeStatus
  return (
    <>
      <SearchBar />
      <SearchResult />
      {showLogs && <Logs />}
      <Test />
    </>
  )
}
