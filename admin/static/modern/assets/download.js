/* SPDX-FileCopyrightText: 2026 basecubedev */
/* SPDX-License-Identifier: AGPL-3.0-or-later */
function e(e,t){let n=URL.createObjectURL(e),r=document.createElement(`a`);r.href=n,r.download=t,document.body.appendChild(r),r.click(),r.remove(),URL.revokeObjectURL(n)}export{e as t};