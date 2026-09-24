<script>
	import '@evidence-dev/tailwind/fonts.css';
	import '../app.css';
	import { EvidenceDefaultLayout } from '@evidence-dev/core-components';
	export let data;
</script>

<EvidenceDefaultLayout
	{data}
	title="Roll Call"
	homePageName="Source status"
	neverShowQueries={true}
	hideBreadcrumbs={true}
>
	<slot slot="content" />
</EvidenceDefaultLayout>

<style>
	/* Keep page titles in sentence case in the sidebar. */
	:global(.capitalize) {
		text-transform: none;
	}
</style>
