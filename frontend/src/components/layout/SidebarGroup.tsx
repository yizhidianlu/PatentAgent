import { useState } from 'react'
import { ChevronDownIcon, EllipsisHorizontalIcon } from '@heroicons/react/24/outline'
import { AnimatePresence, motion } from 'framer-motion'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { cn } from '../../lib/cn'
import { zh } from '../../i18n/zh'
import { useUiStore } from '../../stores/uiStore'
import { useDeleteCase, useRenameCase } from '../../api/sessions'
import { Button } from '../ui/Button'
import { Dropdown, DropdownItem } from '../ui/Dropdown'
import { Input } from '../ui/Input'
import { Modal } from '../ui/Modal'
import { Spinner } from '../ui/Spinner'

export interface SidebarSessionItem {
  id: string
  title: string
  /** 有待确认门控 → 红点。 */
  pendingConfirm?: boolean
}

export interface SidebarGroupProps {
  groupKey: string
  label: string
  /** 条目路由前缀，如 `/disclosure`。 */
  basePath: string
  items: SidebarSessionItem[]
  /** 条目点击后回调（移动抽屉关闭用）。 */
  onNavigate?: () => void
}

/** §2.2 分组：组头（Chevron 旋转 + 计数徽章）+ 条目（active 高亮、待确认红点、hover … 菜单）。 */
export function SidebarGroup({ groupKey, label, basePath, items, onNavigate }: SidebarGroupProps) {
  const collapsedGroups = useUiStore((s) => s.collapsedGroups)
  const toggleGroup = useUiStore((s) => s.toggleGroup)
  const pushToast = useUiStore((s) => s.pushToast)
  const isCollapsed = collapsedGroups.includes(groupKey)

  const navigate = useNavigate()
  const location = useLocation()
  const rename = useRenameCase()
  const remove = useDeleteCase()

  const [renameTarget, setRenameTarget] = useState<SidebarSessionItem | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<SidebarSessionItem | null>(null)
  /** 展开/收起动画进行中（决定列表是否需要 overflow-hidden，见下方注释）。 */
  const [animating, setAnimating] = useState(false)

  const openRename = (item: SidebarSessionItem) => {
    setRenameDraft(item.title)
    setRenameTarget(item)
  }

  const submitRename = () => {
    const target = renameTarget
    const title = renameDraft.trim()
    if (!target || !title) return
    setRenameTarget(null)
    void rename
      .mutateAsync({ id: target.id, title })
      .then(() => pushToast('success', zh.sidebar.renameOk))
      .catch((e: unknown) => {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.sidebar.renameFailed)
      })
  }

  const submitDelete = () => {
    const target = deleteTarget
    if (!target) return
    setDeleteTarget(null)
    void remove
      .mutateAsync(target.id)
      .then(() => {
        pushToast('success', zh.sidebar.deleteOk)
        // 删掉的正是当前打开的会话 → 回工作台，避免停在 404 的案件页
        if (location.pathname === `${basePath}/${target.id}`) navigate('/')
      })
      .catch((e: unknown) => {
        const message = e instanceof Error ? e.message : ''
        pushToast('error', message || zh.sidebar.deleteFailed)
      })
  }

  return (
    <div className="mb-1">
      <button
        type="button"
        onClick={() => toggleGroup(groupKey)}
        aria-expanded={!isCollapsed}
        className="w-full flex items-center gap-1.5 px-2 py-1.5 text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
      >
        <ChevronDownIcon
          className={cn('w-3.5 h-3.5 transition-transform duration-200', isCollapsed && '-rotate-90')}
          strokeWidth={1.5}
        />
        <span className="flex-1 text-left truncate">{label}</span>
        <span className="text-[10px] min-w-[16px] text-center rounded-full px-1 bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
          {items.length}
        </span>
      </button>

      <AnimatePresence initial={false}>
        {!isCollapsed && (
          <motion.ul
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            onAnimationStart={() => setAnimating(true)}
            onAnimationComplete={() => setAnimating(false)}
            /*
             * overflow-hidden 只在展开/收起动画期间需要（裁掉 height 过渡的溢出内容）。
             * 动画结束后必须放开：否则最后一行的 … 菜单面板向下展开会被 ul 裁掉
             * （实测溢出 87px，整块菜单不可见也点不中）。
             */
            className={cn('space-y-0.5', animating ? 'overflow-hidden' : 'overflow-visible')}
          >
            {items.length === 0 && (
              <li className="px-2 py-1.5 text-[11px] leading-relaxed text-gray-400 dark:text-gray-500">
                {zh.sidebar.groupEmpty(label)}
              </li>
            )}
            {items.map((item) => (
              <li key={item.id} className="group/item relative">
                <NavLink
                  to={`${basePath}/${item.id}`}
                  onClick={onNavigate}
                  title={item.title}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center gap-2 pl-2 pr-8 py-2 rounded-lg text-[13px] text-gray-700 dark:text-gray-300 ' +
                        'hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors',
                      isActive && 'bg-gray-200/70 dark:bg-gray-700/70 font-medium text-gray-900 dark:text-white',
                    )
                  }
                >
                  <span className="flex-1 truncate">{item.title}</span>
                  {item.pendingConfirm && (
                    <span
                      title={zh.sidebar.pendingConfirm}
                      aria-label={zh.sidebar.pendingConfirm}
                      role="img"
                      className="min-w-[14px] h-3.5 px-0.5 rounded-full bg-red-500 text-white text-[9px] font-bold ring-2 ring-white dark:ring-gray-900 flex items-center justify-center shrink-0"
                    >
                      1
                    </span>
                  )}
                </NavLink>
                {/*
                  hover 显示 … 菜单（重命名/删除）。
                  `has-[[aria-expanded=true]]:opacity-100` 不可省：菜单展开后鼠标要下移到
                  菜单项上，此时已离开本行，只靠 group-hover 会让整个容器回到 opacity-0 ——
                  容器变透明同时形成层叠上下文，菜单被下一行盖住且点不中（菜单等于不可用）。
                */}
                <div
                  className={cn(
                    'absolute right-1.5 top-1/2 -translate-y-1/2 transition-opacity',
                    // -translate-y-1/2 的 transform 会在此建立层叠上下文，把面板的
                    // z-[60] 关在里面；容器自身必须带 z-index，否则后续兄弟 <li>
                    // （position:relative + z-auto）按树序盖在面板上，菜单项点不中。
                    'z-[var(--z-dropdown)]',
                    'opacity-0 group-hover/item:opacity-100 focus-within:opacity-100',
                    'has-[[aria-expanded=true]]:opacity-100',
                  )}
                >
                  <Dropdown
                    align="right"
                    panelClassName="w-36"
                    trigger={({ toggle, open }) => (
                      <button
                        type="button"
                        aria-label={zh.sidebar.itemMenu.label(item.title)}
                        aria-haspopup="menu"
                        aria-expanded={open}
                        onClick={(e) => {
                          e.preventDefault()
                          toggle()
                        }}
                        className="w-6 h-6 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700 hover:text-gray-600 dark:hover:text-gray-200 transition-colors"
                      >
                        <EllipsisHorizontalIcon className="w-4 h-4" strokeWidth={1.5} />
                      </button>
                    )}
                  >
                    {({ close }) => (
                      <>
                        <DropdownItem
                          onClick={() => {
                            close()
                            openRename(item)
                          }}
                        >
                          {zh.sidebar.itemMenu.rename}
                        </DropdownItem>
                        <DropdownItem
                          danger
                          onClick={() => {
                            close()
                            setDeleteTarget(item)
                          }}
                        >
                          {zh.sidebar.itemMenu.delete}
                        </DropdownItem>
                      </>
                    )}
                  </Dropdown>
                </div>
              </li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>

      {/* 重命名 */}
      <Modal
        open={renameTarget !== null}
        onClose={() => setRenameTarget(null)}
        title={zh.sidebar.itemMenu.rename}
      >
        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="block text-[12px] text-gray-500 dark:text-gray-400">
              {zh.sidebar.renameLabel}
            </span>
            <Input
              value={renameDraft}
              autoFocus
              maxLength={120}
              onChange={(e) => setRenameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  submitRename()
                }
              }}
            />
          </label>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setRenameTarget(null)}>
              {zh.common.cancel}
            </Button>
            <Button
              size="sm"
              onClick={submitRename}
              disabled={!renameDraft.trim() || rename.isPending}
              className="gap-1.5"
            >
              {rename.isPending && <Spinner size="sm" className="border-white/40 border-t-white" />}
              {zh.common.save}
            </Button>
          </div>
        </div>
      </Modal>

      {/* 删除确认 */}
      <Modal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title={zh.sidebar.deleteConfirmTitle}
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-600 dark:text-gray-300">
            {zh.sidebar.deleteConfirmDesc(deleteTarget?.title ?? '')}
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(null)}>
              {zh.common.cancel}
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={submitDelete}
              disabled={remove.isPending}
              className="gap-1.5"
            >
              {remove.isPending && <Spinner size="sm" className="border-white/40 border-t-white" />}
              {zh.common.delete}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
