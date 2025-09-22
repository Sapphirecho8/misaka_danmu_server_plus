import { Domain } from './Domain'
import { Token } from './Token'
import { Ua } from './Ua'
import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../../store/index.js'

export const TokenManage = () => {
  const userinfo = useAtomValue(userinfoAtom)
  const perms = userinfo?.permissions || {}
  const isSuper = (userinfo?.username || '').toLowerCase() === 'admin'
  const canEditCustomDomain = isSuper || !!perms.editCustomDomain
  const canEditUaFilter = isSuper || !!perms.editUaFilter
  return (
    <>
      <Token />
      {canEditCustomDomain && <Domain />}
      {canEditUaFilter && <Ua />}
      <p>
        本项目参考了
        <a
          href="https://api.dandanplay.net/swagger/index.html"
          target="_blank"
          className="text-primary"
          rel="noopener noreferrer"
        >
          dandanplayapi
        </a>
        ，同时增加了使用访问令牌管理弹幕api,支持
        <a
          href="https://t.me/yamby_release"
          target="_blank"
          className="text-primary"
          rel="noopener noreferrer"
        >
          yamby
        </a>
        、
        <a
          href="https://play.google.com/store/search?q=hills&c=apps"
          target="_blank"
          className="text-primary"
          rel="noopener noreferrer"
        >
          hills
        </a>
        、
        <a
          href="https://apps.microsoft.com/detail/9NB0H051M4V4"
          target="_blank"
          className="text-primary"
          rel="noopener noreferrer"
        >
          小幻影视
        </a>
        。
      </p>
    </>
  )
}
