import { debounce } from 'lodash'
import { useRef } from 'react'

/** 函数时组件的防抖 */
/** 注意：一般用于接口防抖
 *
 * @param fn 防抖函数
 * @param DependencyList useCallback依赖
 * @param wait 防抖函数时间间隔
 * @options {
 *  使用场景：
 *  1.希望第一次立即调用，防抖结束后不再调用  （leading:true，trailing:false);
 *  2.希望第一次不调用，防抖结束后立即调用一次（leading:false，trailing:true)
 *  @leading 指定在延迟开始前调用，默认false
 *  @trailing 指定在延迟结束后调用，默认true
 *
 * }
 * @returns useCallback-memorized函数
 */
export const useDebounce = (
  fn,
  wait = 1000,
  options = {
    leading: false,
    trailing: true,
  }
) => {
  const fnRef = useRef(fn)
  const debouncedRef = useRef()
  fnRef.current = fn
  if (!debouncedRef.current) {
    debouncedRef.current = debounce(
      (...args) => fnRef.current(...args),
      wait,
      options
    )
  }

  return debouncedRef.current
}
